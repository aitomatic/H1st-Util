from __future__ import division, print_function

import math
import numpy
import os
import pandas
import psutil
import random
import re
import time
import tqdm
import types
import uuid

import six
if six.PY2:
    from urlparse import urlparse
    _STR_CLASSES = str, unicode
else:
    from urllib.parse import urlparse
    _STR_CLASSES = str

from pyarrow.parquet import ParquetDataset, read_table

from pyspark.ml import Transformer
from pyspark.ml.feature import SQLTransformer
from pyspark.sql import DataFrame

import arimo.backend
from arimo.df import _ADFABC
from arimo.df.from_files import _ArrowADFABC
from arimo.df.spark import SparkADF
from arimo.util import fs, Namespace
from arimo.util.aws import s3
from arimo.util.date_time import gen_aux_cols
from arimo.util.decor import enable_inplace
from arimo.util.iterables import to_iterable
from arimo.util.types.spark_sql import _STR_TYPE
import arimo.debug


# https://stackoverflow.com/questions/12019961/python-pickling-nested-functions
class _ArrowSparkADF__getitem__pandasDFTransform:
    def __init__(self, item):
        self.item = item

    def __call__(self, pandasDF):
        return pandasDF[to_iterable(self.item, iterable_type=list)]


class _ArrowSparkADF__fillna__pandasDFTransform:
    def __init__(self, nullFillDetails):
        self.nullFillDetails = nullFillDetails

    def __call__(self, pandasDF):
        for col, nullFillColNameNDetails in self.nullFillDetails.items():
            if (col not in ('__TS_WINDOW_CLAUSE__', '__SCALER__')) and \
                    isinstance(nullFillColNameNDetails, list) and (len(nullFillColNameNDetails) == 2):
                _, nullFill = nullFillColNameNDetails

                lowerNull, upperNull = nullFill['Nulls']

                series = pandasDF[col]

                chks = series.notnull()

                if lowerNull is not None:
                    chks &= (series > lowerNull)

                if upperNull is not None:
                    chks &= (series < upperNull)

                pandasDF[_ADFABC._NULL_FILL_PREFIX + col + _ADFABC._PREP_SUFFIX] = \
                    pandasDF[col].where(
                        cond=chks,
                        other=nullFill['NullFillValue'],
                        inplace=False,
                        axis=None,
                        level=None,
                        errors='raise',
                        try_cast=False)

        return pandasDF


class _ArrowSparkADF__prep__pandasDFTransform:
    def __init__(self, sparkTypes, catOrigToPrepColMap, numOrigToPrepColMap):
        self.sparkTypes = sparkTypes

        assert not catOrigToPrepColMap['__OHE__']
        self.catOrigToPrepColMap = catOrigToPrepColMap
        self.scaleCat = catOrigToPrepColMap['__SCALE__']

        self.numOrigToPrepColMap = numOrigToPrepColMap
        self.numScaler = numOrigToPrepColMap['__SCALER__']
        assert self.numScaler in ('standard', 'maxabs', 'minmax', None)

    def __call__(self, pandasDF):
        _FLOAT_ABS_TOL = 1e-6

        for catCol, prepCatColNameNDetails in self.catOrigToPrepColMap.items():
            if (catCol not in ('__OHE__', '__SCALE__')) and \
                    isinstance(prepCatColNameNDetails, list) and (len(prepCatColNameNDetails) == 2):
                prepCatCol, catColDetails = prepCatColNameNDetails

                cats = catColDetails['Cats']
                nCats = catColDetails['NCats']

                s = pandasDF[catCol]

                pandasDF[prepCatCol] = \
                    (sum(((s == cat) * i)
                         for i, cat in enumerate(cats)) +
                     ((~s.isin(cats)) * nCats)) \
                    if self.sparkTypes[catCol] == _STR_TYPE \
                    else (sum((((s - cat).abs() < _FLOAT_ABS_TOL) * i)
                              for i, cat in enumerate(cats)) +
                          ((1 -
                            sum(((s - cat).abs() < _FLOAT_ABS_TOL)
                                for cat in cats)) *
                           nCats))

                if self.scaleCat:
                    pandasDF[prepCatCol] = minMaxScaledIdxSeries = \
                        2 * pandasDF[prepCatCol] / nCats - 1

                    assert minMaxScaledIdxSeries.between(left=-1, right=1, inclusive=True).all(), \
                        '*** "{}" CERTAIN MIN-MAX SCALED INT INDICES NOT BETWEEN -1 AND 1 ***'

        pandasDF = _ArrowSparkADF__fillna__pandasDFTransform(nullFillDetails=self.numOrigToPrepColMap)(pandasDF=pandasDF)

        for numCol, prepNumColNameNDetails in self.numOrigToPrepColMap.items():
            if (numCol not in ('__TS_WINDOW_CLAUSE__', '__SCALER__')) and \
                    isinstance(prepNumColNameNDetails, list) and (len(prepNumColNameNDetails) == 2):
                prepNumCol, numColDetails = prepNumColNameNDetails

                nullFillColSeries = \
                    pandasDF[_ADFABC._NULL_FILL_PREFIX + numCol + _ADFABC._PREP_SUFFIX]

                if self.numScaler == 'standard':
                    pandasDF[prepNumCol] = \
                        (nullFillColSeries - numColDetails['Mean']) / numColDetails['StdDev']

                elif self.numScaler == 'maxabs':
                    pandasDF[prepNumCol] = \
                        nullFillColSeries / numColDetails['MaxAbs']

                elif self.numScaler == 'minmax':
                    origMin = numColDetails['OrigMin']
                    origMax = numColDetails['OrigMax']
                    origRange = origMax - origMin

                    targetMin = numColDetails['TargetMin']
                    targetMax = numColDetails['TargetMax']
                    targetRange = targetMax - targetMin

                    pandasDF[prepNumCol] = \
                        targetRange * (nullFillColSeries - origMin) / origRange + targetMin

        return pandasDF


class _ArrowSparkADF__drop__pandasDFTransform:
    def __init__(self, cols):
        self.cols = list(cols)

    def __call__(self, pandasDF):
        return pandasDF.drop(
                columns=self.cols,
                level=None,
                inplace=False,
                errors='ignore')


_PIECE_LOCAL_CACHE_PATHS = {}


class _ArrowSparkADF__pieceArrowTableFunc:
    def __init__(self, path, aws_access_key_id=None, aws_secret_access_key=None, n_threads=1):
        self.path = path

        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key

        self.n_threads = n_threads

    def __call__(self, pieceSubPath):
        path = os.path.join(self.path, pieceSubPath)

        if self.path.startswith('s3'):
            global _PIECE_LOCAL_CACHE_PATHS

            if path in _PIECE_LOCAL_CACHE_PATHS:
                path = _PIECE_LOCAL_CACHE_PATHS[path]

            else:
                parsedURL = \
                    urlparse(
                        url=path,
                        scheme='',
                        allow_fragments=True)

                _PIECE_LOCAL_CACHE_PATHS[path] = path = \
                    os.path.join(
                        _ADFABC._TMP_DIR_PATH,
                        parsedURL.netloc,
                        parsedURL.path[1:])

                fs.mkdir(
                    dir=os.path.dirname(path),
                    hdfs=False)

                s3.client(
                        access_key_id=self.aws_access_key_id,
                        secret_access_key=self.aws_secret_access_key) \
                    .download_file(
                        Bucket=parsedURL.netloc,
                        Key=parsedURL.path[1:],
                        Filename=path)

        return read_table(
                source=path,
                columns=None,
                nthreads=self.n_threads,
                metadata=None,
                use_pandas_metadata=False)


class _ArrowSparkADF__gen:
    def __init__(
            self, args,
            path,
            pieceSubPaths,
            aws_access_key_id, aws_secret_access_key,
            iCol, tCol,
            possibleFeatureTAuxCols, contentCols,
            pandasDFTransforms,
            filterConditions,
            n, sampleN,
            anon,
            n_threads):
        def cols_rowFrom_rowTo(x):
            if isinstance(x, _STR_CLASSES):
                return [x], None, None
            elif isinstance(x, (list, tuple)) and x:
                lastItem = x[-1]
                if isinstance(lastItem, _STR_CLASSES):
                    return x, None, None
                elif isinstance(lastItem, int):
                    secondLastItem = x[-2]
                    return ((x[:-1], 0, lastItem)
                            if lastItem >= 0
                            else (x[:-1], lastItem, 0)) \
                        if isinstance(secondLastItem, _STR_CLASSES) \
                        else (x[:-2], secondLastItem, lastItem)

        self.pieceSubPaths = list(pieceSubPaths)

        self.n_threads = n_threads

        self.pieceArrowTableFunc = \
            _ArrowSparkADF__pieceArrowTableFunc(
                path=path,
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key,
                n_threads=n_threads)

        self.filterConditions = filterConditions

        if filterConditions and arimo.debug.ON:
            print('*** FILTER CONDITION: {} ***'.format(filterConditions))

        self.n = n
        self.sampleN = sampleN

        self.anon = anon

        self.iCol = iCol
        self.tCol = tCol

        self.hasTS = self.iCol and self.tCol

        self.possibleFeatureTAuxCols = possibleFeatureTAuxCols
        self.contentCols = contentCols

        self.pandasDFTransforms = pandasDFTransforms

        _hasTS = False
        minTOrd = 0

        if args:
            if self.hasTS:
                self.colsLists = []
                self.colsOverTime = []
                self.rowFrom_n_rowTo_tups = []

                for cols, rowFrom, rowTo in map(cols_rowFrom_rowTo, args):
                    self.colsLists.append(cols)

                    if (rowFrom is None) and (rowTo is None):
                        self.colsOverTime.append(False)
                        self.rowFrom_n_rowTo_tups.append(None)

                    else:
                        assert (rowFrom < rowTo) and (rowTo <= 0)

                        self.colsOverTime.append(True)
                        self.rowFrom_n_rowTo_tups.append((rowFrom, rowTo))

                        _hasTS = True

                        if -rowFrom > minTOrd:
                            minTOrd = -rowFrom

            else:
                self.colsLists = list(args)
                nArgs = len(args)
                self.colsOverTime = nArgs * [False]
                self.rowFrom_n_rowTo_tups = nArgs * [None]

        else:
            self.colsLists = [self.possibleFeatureTAuxCols + self.contentCols]
            self.colsOverTime = [False]
            self.rowFrom_n_rowTo_tups = [None]

        if _hasTS:
            self.filterConditions[_ADFABC._T_ORD_COL] = minTOrd, numpy.inf

        if (not self.anon) and (self.iCol or self.tCol):
            self.colsLists.insert(0, ([self.iCol] if self.iCol else []) + ([self.tCol] if self.tCol else []))
            self.colsOverTime.insert(0, False)
            self.rowFrom_n_rowTo_tups.insert(0, None)

    def __call__(self):
        if arimo.debug.ON:
            print('*** GENERATING BATCHES OF {} ***'.format(self.colsLists))

        while True:
            pieceSubPath = random.choice(self.pieceSubPaths)

            chunkPandasDF = \
                random.choice(
                    self.pieceArrowTableFunc(pieceSubPath=pieceSubPath)
                        .to_batches(chunksize=self.sampleN)) \
                .to_pandas(
                    nthreads=self.n_threads)

            if self.tCol:
                chunkPandasDF = \
                    gen_aux_cols(
                        df=chunkPandasDF,
                        i_col=self.iCol,
                        t_col=self.tCol)

            for i, pandasDFTransform in enumerate(self.pandasDFTransforms):
                try:
                    chunkPandasDF = pandasDFTransform(chunkPandasDF)

                except Exception as err:
                    print('*** "{}": PANDAS TRANSFORM #{} ***'.format(pieceSubPath, i))
                    raise err

            if self.filterConditions:
                filterChunkPandasDF = chunkPandasDF[list(self.filterConditions)]

                rowIndices = \
                    filterChunkPandasDF.loc[
                        sum((filterChunkPandasDF[filterCol]
                                .between(
                                    left=left,
                                    right=right,
                                    inclusive=True)
                             if pandas.notnull(left) and pandas.notnull(right)
                             else ((filterChunkPandasDF[filterCol] >= left)
                                    if pandas.notnull(left)
                                    else ((filterChunkPandasDF[filterCol] <= right))))
                            for filterCol, (left, right) in self.filterConditions.items())
                        == len(self.filterConditions)] \
                    .index.tolist()

            else:
                rowIndices = chunkPandasDF.index.tolist()

            random.shuffle(rowIndices)

            n_batches = len(rowIndices) // self.n

            for i in range(n_batches):
                rowIndicesSubset = rowIndices[(i * self.n):((i + 1) * self.n)]

                yield [(numpy.vstack(
                            numpy.expand_dims(
                                chunkPandasDF.loc[(rowIdx + rowFrom_n_rowTo[0]):(rowIdx + rowFrom_n_rowTo[1] + 1), cols].values,
                                axis=0)
                            for rowIdx in rowIndicesSubset)
                        if overTime
                        else chunkPandasDF.loc[rowIndicesSubset, cols].values)
                       for cols, overTime, rowFrom_n_rowTo in
                            zip(self.colsLists, self.colsOverTime, self.rowFrom_n_rowTo_tups)]


@enable_inplace
class ArrowSparkADF(_ArrowADFABC, SparkADF):
    # "inplace-able" methods
    _INPLACE_ABLE = \
        '__call__', \
        '_subset', \
        'drop', \
        'fillna', \
        'filter', \
        'filterByPartitionKeys', \
        'prep', \
        'select', \
        'sql', \
        'transform', \
        'withColumn'

    _CACHE = {}

    # *****************
    # METHODS TO CREATE
    # __init__
    # load
    # read

    def __init__(
            self, path, aws_access_key_id=None, aws_secret_access_key=None, reCache=False,
            _srcSparkDFSchema=None, _initSparkDF=None, _sparkDFTransforms=[], _sparkDF=None,
            _pandasDFTransforms=[],
            reprSampleNPieces=_ArrowADFABC._DEFAULT_REPR_SAMPLE_N_PIECES,
            verbose=True, **kwargs):
        if verbose or arimo.debug.ON:
            logger = self.class_stdout_logger()

        self.path = path

        if (not reCache) and (path in self._CACHE):
            _cache = self._CACHE[path]
            
        else:
            self._CACHE[path] = _cache = Namespace()

        if 'detPrePartitioned' not in kwargs:
            kwargs['detPrePartitioned'] = True

        if _cache:
            if arimo.debug.ON:
                logger.debug('*** RETRIEVING CACHE FOR "{}" ***'.format(path))

        else:
            if verbose:
                msg = 'Loading Arrow Dataset from "{}"...'.format(path)
                logger.info(msg)
                tic = time.time()

            _cache._srcArrowDS = \
                ParquetDataset(
                    path_or_paths=path,
                    filesystem=
                        self._s3FS(
                            key=aws_access_key_id,
                            secret=aws_secret_access_key)
                        if path.startswith('s3')
                        else (self._HDFS_ARROW_FS
                              if fs._ON_LINUX_CLUSTER_WITH_HDFS
                              else self._LOCAL_ARROW_FS),
                    schema=None, validate_schema=False, metadata=None,
                    split_row_groups=False)

            if verbose:
                toc = time.time()
                logger.info(msg + ' done!   <{:,.1f} s>'.format(toc - tic))

            _cache.nPieces = len(_cache._srcArrowDS.pieces)

            if _cache.nPieces:
                _cache.piecePaths = set()

                _pathPlusSepLen = len(path) + 1

                _cache.pieceSubPaths = set()

                for i, piece in enumerate(_cache._srcArrowDS.pieces):
                    piecePath = piece.path
                    _cache.piecePaths.add(piecePath)

                    pieceSubPath = piecePath[_pathPlusSepLen:]
                    _cache.pieceSubPaths.add(pieceSubPath)

                    if not i:
                        _cache._partitionedByDateOnly = \
                            pieceSubPath.startswith('{}='.format(self._DEFAULT_D_COL)) and \
                            (pieceSubPath.count('/') == 1)

            else:
                _cache.nPieces = 1
                _cache.piecePaths = {path}
                _cache.pieceSubPaths = {}
                _cache._partitionedByDateOnly = False

            if path.startswith('s3'):
                _cache.s3Client = \
                    s3.client(
                        access_key_id=aws_access_key_id,
                        secret_access_key=aws_secret_access_key)

                _parsedURL = urlparse(url=path, scheme='', allow_fragments=True)
                _cache.s3Bucket = _parsedURL.netloc
                _cache.pathS3Key = _parsedURL.path[1:]

                _cache.tmpDirS3Key = self._TMP_DIR_PATH.strip('/')

                _cache.tmpDirPath = \
                    os.path.join(
                        's3://{}'.format(_cache.s3Bucket),
                        _cache.tmpDirS3Key)

                path = s3.s3a_path_with_auth(
                        s3_path=path,
                        access_key_id=aws_access_key_id,
                        secret_access_key=aws_secret_access_key)

            else:
                _cache.s3Client = _cache.s3Bucket = _cache.tmpDirS3Key = None
                _cache.tmpDirPath = self._TMP_DIR_PATH

            if arimo.backend.chkSpark():
                if kwargs['detPrePartitioned']:
                    arimo.backend.spark.conf.set(
                        'spark.files.maxPartitionBytes',
                        arimo.backend._MAX_JAVA_INTEGER)

                    arimo.backend.spark.conf.set(
                        'spark.sql.files.maxPartitionBytes',
                        arimo.backend._MAX_JAVA_INTEGER)

                    arimo.backend.spark.conf.set(
                        'spark.files.openCostInBytes',
                        arimo.backend._MAX_JAVA_INTEGER)

                    arimo.backend.spark.conf.set(
                        'spark.sql.files.openCostInBytes',
                        arimo.backend._MAX_JAVA_INTEGER)

                else:
                    arimo.backend.spark.conf.set(
                        'spark.files.maxPartitionBytes',
                        arimo.backend._SPARK_CONF['spark.files.maxPartitionBytes'])

                    arimo.backend.spark.conf.set(
                        'spark.sql.files.maxPartitionBytes',
                        arimo.backend._SPARK_CONF['spark.sql.files.maxPartitionBytes'])

                    arimo.backend.spark.conf.set(
                        'spark.files.openCostInBytes',
                        arimo.backend._SPARK_CONF['spark.files.openCostInBytes'])

                    arimo.backend.spark.conf.set(
                        'spark.sql.files.openCostInBytes',
                        arimo.backend._SPARK_CONF['spark.sql.files.openCostInBytes'])

            else:
                sparkConf = kwargs.pop('sparkConf', {})

                if kwargs['detPrePartitioned']:
                    sparkConf['spark.files.maxPartitionBytes'] = \
                        sparkConf['spark.sql.files.maxPartitionBytes'] = \
                        sparkConf['spark.files.openCostInBytes'] = \
                        sparkConf['spark.sql.files.openCostInBytes'] = \
                        arimo.backend._MAX_JAVA_INTEGER

                arimo.backend.initSpark(sparkConf=sparkConf)

            if verbose:
                msg = 'Loading SparkDF from "{}"...'.format(self.path)
                logger.info(msg)
                tic = time.time()

            _cache._srcSparkDF = \
                arimo.backend.spark.read.load(
                    path=path,
                    format='parquet',
                    schema=_srcSparkDFSchema)

            _cache._srcNRows = _cache._srcSparkDF.count()

            _cache._srcSparkDFSchema = _cache._srcSparkDF.schema

            if verbose:
                toc = time.time()
                logger.info(msg + ' done!   <{:,.1f} s>'.format(toc - tic))

        self.__dict__.update(_cache)

        alias = kwargs.pop('alias', None)
            
        if _initSparkDF:
            super(ArrowSparkADF, self).__init__(
                sparkDF=_initSparkDF,
                **kwargs)

        else:
            super(ArrowSparkADF, self).__init__(
                sparkDF=self._srcSparkDF,
                nRows=self._srcNRows,
                **kwargs)

        self._initSparkDF = self._sparkDF

        self._sparkDFTransforms = _sparkDFTransforms

        self._pandasDFTransforms = _pandasDFTransforms

        if _sparkDF is None:
            if _sparkDFTransforms:
                for i, sparkDFTransform in enumerate(_sparkDFTransforms):
                    try:
                        self._sparkDF = \
                            sparkDFTransform.transform(dataset=self._sparkDF) \
                            if isinstance(sparkDFTransform, Transformer) \
                            else sparkDFTransform(self._sparkDF)


                    except Exception as err:
                        self.stdout_logger.error(
                            msg='*** {} TRANSFORM #{}: ***'
                                .format(self.path, i))
                        raise err

                _recacheTypes = True

            else:
                _recacheTypes = False

        else:
            self._sparkDF = _sparkDF
            _recacheTypes = True

        if alias:
            self.alias = alias

        if _recacheTypes:
            self._cache.type = \
                Namespace(**
                    {col: type
                     for col, type in self.dtypes})

        self._reprSampleNPieces = min(reprSampleNPieces, self.nPieces)

        self._cache.pieceADFs = {}

    @classmethod
    def load(cls, path, **kwargs):
        return cls(path=path, **kwargs)

    # ********************************
    # "INTERNAL / DON'T TOUCH" METHODS
    # _inplace

    def _inplace(self, adf, alias=None):
        if isinstance(adf, (tuple, list)):   # just in case we're taking in multiple inputs
            adf = adf[0]

        assert isinstance(adf, ArrowSparkADF)

        self.path = adf.path

        self.__dict__.update(self._CACHE[adf.path])

        self._initSparkDF = adf._initSparkDF
        self._sparkDFTransforms = adf._sparkDFTransforms
        self._pandasDFTransforms = adf._pandasDFTransforms
        self._sparkDF = adf._sparkDF

        self.alias = alias \
            if alias \
            else (self._alias
                  if self._alias
                  else adf._alias)

        self._cache = adf._cache

    # **********************
    # PYTHON DEFAULT METHODS
    # __dir__
    # __getattr__
    # __getitem__
    # __repr__
    # __short_repr__

    def __dir__(self):
        return sorted(set(
            dir(type(self)) +
            self.__dict__.keys() +
            dir(DataFrame) +
            dir(self._sparkDF)))

    def __getitem__(self, item):
        return self.transform(
                sparkDFTransform=
                    lambda sparkDF:
                        sparkDF[item],
                pandasDFTransform=_ArrowSparkADF__getitem__pandasDFTransform(item=item),
                inheritCache=True,
                inheritNRows=True) \
            if isinstance(item, (list, tuple)) \
          else super(ArrowSparkADF, self).__getitem__(item)

    def __repr__(self):
        cols = self.columns

        cols_and_types_str = []

        if self._iCol in cols:
            cols_and_types_str += ['(iCol) {}: {}'.format(self._iCol, self._cache.type[self._iCol])]

        if self._dCol in cols:
            cols_and_types_str += ['(dCol) {}: {}'.format(self._dCol, self._cache.type[self._dCol])]

        if self._tCol in cols:
            cols_and_types_str += ['(tCol) {}: {}'.format(self._tCol, self._cache.type[self._tCol])]

        cols_and_types_str += \
            ['{}: {}'.format(col, self._cache.type[col])
             for col in self.contentCols]

        return '{}{:,}-piece {:,}-partition {}{}{}["{}" + {} transform(s)][{}]'.format(
            '"{}" '.format(self._alias)
                if self._alias
                else '',
            self.nPieces,
            self.nPartitions,
            '' if self._cache.nRows is None
               else '{:,}-row '.format(self._cache.nRows),
            '(cached) '
                if self.is_cached
                else '',
            type(self).__name__,
            self.path,
            len(self._sparkDFTransforms),
            ', '.join(cols_and_types_str))

    @property
    def __short_repr__(self):
        cols = self.columns

        cols_desc_str = []

        if self._iCol in cols:
            cols_desc_str += ['iCol: {}'.format(self._iCol)]

        if self._dCol in cols:
            cols_desc_str += ['dCol: {}'.format(self._dCol)]

        if self._tCol in cols:
            cols_desc_str += ['tCol: {}'.format(self._tCol)]

        cols_desc_str += ['{} content col(s)'.format(len(self.contentCols))]

        return '{}{:,}-piece {:,}-partition {}{}{}[{} transform(s)][{}]'.format(
            '"{}" '.format(self._alias)
                if self._alias
                else '',
            self.nPieces,
            self.nPartitions,
            '' if self._cache.nRows is None
               else '{:,}-row '.format(self._cache.nRows),
            '(cached) '
                if self.is_cached
                else '',
            type(self).__name__,
            len(self._sparkDFTransforms),
            ', '.join(cols_desc_str))

    # **********
    # TRANSFORMS
    # transform
    # select
    # sql
    # __call__
    # fillna
    # prep
    # drop
    # filter
    # withColumn

    def transform(self, sparkDFTransform, _sparkDF=None, pandasDFTransform=[], *args, **kwargs):
        stdKwArgs = self._extractStdKwArgs(kwargs, resetToClassDefaults=False, inplace=False)

        if stdKwArgs.alias and (stdKwArgs.alias == self.alias):
            stdKwArgs.alias = None

        inheritCache = kwargs.pop('inheritCache', False)

        if isinstance(sparkDFTransform, list):
            additionalSparkDFTransforms = sparkDFTransform

            inheritCache |= \
                all(isinstance(additionalSparkDFTransform, Transformer)
                    for additionalSparkDFTransform in additionalSparkDFTransforms)

        elif isinstance(sparkDFTransform, Transformer):
            additionalSparkDFTransforms = [sparkDFTransform]
            inheritCache = True

        else:
            additionalSparkDFTransforms = \
                [(lambda sparkDF: sparkDFTransform(sparkDF, *args, **kwargs))
                 if args or kwargs
                 else sparkDFTransform]

        additionalPandasDFTransforms = \
            pandasDFTransform \
            if isinstance(pandasDFTransform, list) \
            else [pandasDFTransform]

        inheritNRows = kwargs.pop('inheritNRows', inheritCache)

        if _sparkDF is None:
            _sparkDF = self._sparkDF

            for i, additionalSparkDFTransform in enumerate(additionalSparkDFTransforms):
                try:
                    _sparkDF = additionalSparkDFTransform.transform(dataset=_sparkDF) \
                        if isinstance(additionalSparkDFTransform, Transformer) \
                        else additionalSparkDFTransform(_sparkDF)

                except Exception as err:
                    self.stdout_logger.error(
                        msg='*** {} ADDITIONAL TRANSFORM #{} ({}): ***'
                            .format(self.path, i, additionalSparkDFTransform))
                    raise err

        adf = ArrowSparkADF(
            path=self.path,
            _initSparkDF=self._initSparkDF,
            _sparkDFTransforms=self._sparkDFTransforms + additionalSparkDFTransforms,
            _pandasDFTransforms=self._pandasDFTransforms + additionalPandasDFTransforms,
            _sparkDF=_sparkDF,
            nRows=self._cache.nRows
                if inheritNRows
                else None,
            **stdKwArgs.__dict__)

        if inheritCache:
            adf._inheritCache(self)

        adf._cache.pieceADFs = self._cache.pieceADFs

        return adf

    def select(self, *exprs, **kwargs):
        if exprs:
            inheritCache = kwargs.pop('inheritCache', '*' in exprs)

        else:
            exprs = '*',
            inheritCache = kwargs.pop('inheritCache', True)

        inheritNRows = kwargs.pop('inheritNRows', inheritCache)

        return self.transform(
            sparkDFTransform=
                (lambda sparkDF: sparkDF.selectExpr(*exprs))
                if all(isinstance(expr, _STR_CLASSES) for expr in exprs)
                else (lambda sparkDF: sparkDF.select(*exprs)),
            pandasDFTransform=[],   # no Pandas equivalent
            inheritCache=inheritCache,
            inheritNRows=inheritNRows,
            **kwargs)

    def sql(self, query='SELECT * FROM this', tempAlias='this', **kwargs):
        origAlias = self._alias
        self.alias = tempAlias

        try:
            _lower_query = query.strip().lower()
            assert _lower_query.startswith('select')

            _sparkDF = arimo.backend.spark.sql(query)
            self.alias = origAlias

            inheritCache = \
                kwargs.pop(
                    'inheritCache',
                    (('select *' in _lower_query) or ('select {}.*'.format(tempAlias.lower()) in _lower_query)) and
                    ('where' not in _lower_query) and ('join' not in _lower_query) and ('union' not in _lower_query))

            inheritNRows = kwargs.pop('inheritNRows', inheritCache)

            return self.transform(
                sparkDFTransform=
                    SQLTransformer(
                        statement=
                            query.replace(' {}'.format(tempAlias), ' __THIS__')
                                 .replace('{} '.format(tempAlias), '__THIS__ ')
                                 .replace('{}.'.format(tempAlias), '__THIS__.')),
                pandasDFTransform=[],   # no Pandas equivalent
                _sparkDF=_sparkDF,
                inheritCache=inheritCache,
                inheritNRows=inheritNRows,
                **kwargs)

        except Exception as exception:
            self.alias = origAlias
            raise exception

    def __call__(self, *args, **kwargs):
        if args:
            arg = args[0]

            if isinstance(arg, Transformer) or \
                    (callable(arg) and (not isinstance(arg, SparkADF)) and (not isinstance(arg, types.ClassType))):
                return self.transform(
                    sparkDFTransform=arg,
                    *(args[1:]
                      if (len(args) > 1)
                      else ()),
                    **kwargs)

            elif (len(args) == 1) and isinstance(arg, _STR_CLASSES) and arg.strip().lower().startswith('select'):
                return self.sql(query=arg, **kwargs)

            else:
                return self.select(*args, **kwargs)

        else:
            return self.sql(**kwargs)

    def fillna(self, *cols, **kwargs):
        stdKwArgs = self._extractStdKwArgs(kwargs, resetToClassDefaults=False, inplace=False)

        if stdKwArgs.alias and (stdKwArgs.alias == self.alias):
            stdKwArgs.alias = None

        returnDetails = kwargs.pop('returnDetails', False)

        kwargs['returnDetails'] = \
            kwargs['returnSQLTransformer'] = True

        adf, nullFillDetails, sqlTransformer = \
            super(ArrowSparkADF, self).fillna(*cols, **kwargs)

        adf = self.transform(
            sparkDFTransform=sqlTransformer,
            pandasDFTransform=_ArrowSparkADF__fillna__pandasDFTransform(nullFillDetails=nullFillDetails),
            _sparkDF=adf._sparkDF,
            inheritCache=True,
            inheritNRows=True,
            **stdKwArgs.__dict__)

        adf._inheritCache(adf)
        adf._cache.reprSample = self._cache.reprSample

        return (adf, nullFillDetails) \
            if returnDetails \
          else adf

    def prep(self, *cols, **kwargs):
        stdKwArgs = self._extractStdKwArgs(kwargs, resetToClassDefaults=False, inplace=False)

        if stdKwArgs.alias and (stdKwArgs.alias == self.alias):
            stdKwArgs.alias = None

        returnOrigToPrepColMaps = \
            kwargs.pop('returnOrigToPrepColMaps', False)

        kwargs['returnOrigToPrepColMaps'] = \
            kwargs['returnPipeline'] = True

        adf, catOrigToPrepColMap, numOrigToPrepColMap, pipelineModel = \
            super(ArrowSparkADF, self).prep(*cols, **kwargs)

        if arimo.debug.ON:
            self.stdout_logger.debug(
                msg='*** ORIG-TO-PREP METADATA: ***\n{}\n{}'
                    .format(catOrigToPrepColMap, numOrigToPrepColMap))

        adf = self.transform(
            sparkDFTransform=pipelineModel,
            pandasDFTransform=
                _ArrowSparkADF__prep__pandasDFTransform(
                    sparkTypes={catCol: self._initSparkDF._schema[str(catCol)].dataType.simpleString()
                                for catCol in set(catOrigToPrepColMap).difference(('__OHE__', '__SCALE__'))},
                    catOrigToPrepColMap=catOrigToPrepColMap,
                    numOrigToPrepColMap=numOrigToPrepColMap),
            _sparkDF=adf._sparkDF,
            inheritCache=True,
            inheritNRows=True,
            **stdKwArgs.__dict__)

        adf._inheritCache(adf)
        adf._cache.reprSample = self._cache.reprSample

        return (adf, catOrigToPrepColMap, numOrigToPrepColMap) \
            if returnOrigToPrepColMaps \
          else adf

    def drop(self, *cols, **kwargs):
        return self.transform(
                sparkDFTransform=
                    lambda sparkDF:
                        sparkDF.drop(*cols),
                pandasDFTransform=_ArrowSparkADF__drop__pandasDFTransform(cols=cols),
                inheritCache=True,
                inheritNRows=True,
                **kwargs)

    def filter(self, condition, **kwargs):
        return self.transform(
                sparkDFTransform=
                    lambda sparkDF:
                        sparkDF.filter(
                            condition=condition),
                pandasDFTransform=[],   # no Pandas equivalent
                inheritCache=True,
                inheritNRows=True,
                **kwargs)

    def withColumn(self, colName, colExpr, **kwargs):
        return self.transform(
                sparkDFTransform=lambda sparkDF: sparkDF.withColumn(colName=colName, col=colExpr),
                pandasDFTransform=[],   # no Pandas equivalent
                inheritCache=True,
                inheritNRows=True,
                **kwargs)

    # **************
    # SUBSET METHODS
    # _subset
    # filterByPartitionKeys
    # sample
    # gen

    def _subset(self, *pieceSubPaths, **kwargs):
        if pieceSubPaths:
            assert self.pieceSubPaths.issuperset(pieceSubPaths)

            nPieceSubPaths = len(pieceSubPaths)

            if nPieceSubPaths == self.nPieces:
                return self

            else:
                verbose = kwargs.pop('verbose', True)

                if self.s3Client:
                    subsetDirS3Key = \
                        os.path.join(
                            self.tmpDirS3Key,
                            str(uuid.uuid4()))

                    subsetDirPath = \
                        os.path.join(
                            's3://{}'.format(self.s3Bucket),
                            subsetDirS3Key)

                    for pieceSubPath in \
                            (tqdm.tqdm(pieceSubPaths)
                             if verbose
                             else pieceSubPaths):
                        self.s3Client.copy(
                            CopySource=dict(
                                Bucket=self.s3Bucket,
                                Key=os.path.join(self.pathS3Key, pieceSubPath)),
                            Bucket=self.s3Bucket,
                            Key=os.path.join(subsetDirS3Key, pieceSubPath))

                    aws_access_key_id = self._srcArrowDS.fs.fs.key
                    aws_secret_access_key = self._srcArrowDS.fs.fs.secret

                else:
                    subsetDirPath = \
                        os.path.join(
                            self.tmpDirPath,
                            str(uuid.uuid4()))

                    for pieceSubPath in \
                            (tqdm.tqdm(pieceSubPaths)
                             if verbose
                             else pieceSubPaths):
                        fs.cp(
                            from_path=os.path.join(self.path, pieceSubPath),
                            to_path=os.path.join(subsetDirPath, pieceSubPath),
                            hdfs=fs._ON_LINUX_CLUSTER_WITH_HDFS, is_dir=False)

                    aws_access_key_id = aws_secret_access_key = None

                stdKwArgs = self._extractStdKwArgs(kwargs, resetToClassDefaults=False, inplace=False)

                if stdKwArgs.alias and (stdKwArgs.alias == self.alias):
                    stdKwArgs.alias = None

                if stdKwArgs.detPrePartitioned:
                    stdKwArgs.nDetPrePartitions = nPieceSubPaths

                adf = ArrowSparkADF(
                    path=subsetDirPath,
                    aws_access_key_id=aws_access_key_id, aws_secret_access_key=aws_secret_access_key,
                    _srcSparkDFSchema=self._srcSparkDFSchema,
                    _sparkDFTransforms=self._sparkDFTransforms,
                    _pandasDFTransforms=self._pandasDFTransforms,
                    verbose=verbose,
                    **stdKwArgs.__dict__)

                adf._cache.colWidth.update(self._cache.colWidth)

                return adf

        else:
            return self

    def _pieceADF(self, pieceSubPath):
        pieceADF = self._cache.pieceADFs.get(pieceSubPath)

        if pieceADF is None:
            if self._partitionedByDateOnly:
                if self.s3Client:
                    aws_access_key_id = self._srcArrowDS.fs.fs.key
                    aws_secret_access_key = self._srcArrowDS.fs.fs.secret

                else:
                    aws_access_key_id = aws_secret_access_key = None

                stdKwArgs = self._extractStdKwArgs({}, resetToClassDefaults=False, inplace=False)

                if stdKwArgs.alias:
                    assert stdKwArgs.alias == self.alias
                    stdKwArgs.alias = None

                if stdKwArgs.detPrePartitioned:
                    stdKwArgs.nDetPrePartitions = 1

                piecePath = os.path.join(self.path, pieceSubPath)

                pieceADF = \
                    ArrowSparkADF(
                        path=piecePath,
                        aws_access_key_id=aws_access_key_id, aws_secret_access_key=aws_secret_access_key,
                        _srcSparkDFSchema=self._srcSparkDFSchema,
                        _sparkDFTransforms=self._sparkDFTransforms,
                        _pandasDFTransforms=self._pandasDFTransforms,
                        verbose=False,
                        **stdKwArgs.__dict__)

                pieceADF._cache.colWidth.update(self._cache.colWidth)

            else:
                pieceADF = self._subset(pieceSubPath, verbose=False)

            self._cache.pieceADFs[pieceSubPath] = pieceADF

        else:
            pieceADF._sparkDFTransforms = sparkDFTransforms = self._sparkDFTransforms

            pieceADF._sparkDF = pieceADF._initSparkDF

            for i, sparkDFTransform in enumerate(sparkDFTransforms):
                try:
                    pieceADF._sparkDF = sparkDFTransform(pieceADF._sparkDF)

                except Exception as err:
                    self.stdout_logger.error(
                        msg='*** {} TRANSFORM #{}: ***'
                            .format(pieceSubPath, i))
                    raise err

            pieceADF._pandasDFTransforms = self._pandasDFTransforms

            pieceADF._cache.type = self._cache.type
            
        return pieceADF

    def _pieceArrowTable(self, pieceSubPath):
        return _ArrowSparkADF__pieceArrowTableFunc(
                path=self.path,
                aws_access_key_id=self._srcArrowDS.fs.fs.key,
                aws_secret_access_key=self._srcArrowDS.fs.fs.secret)(pieceSubPath)

    def _piecePandasDF(self, pieceSubPath):
        pandasDF = \
            self._pieceArrowTable(pieceSubPath) \
                .to_pandas(
                    nthreads=max(1, psutil.cpu_count(logical=True) // 2),
                    strings_to_categorical=False,
                    memory_pool=None,
                    zero_copy_only=True)

        if self._tCol:
            pandasDF = \
                gen_aux_cols(
                    df=pandasDF,
                    i_col=self._iCol,
                    t_col=self._tCol)

        for i, pandasDFTransform in enumerate(self._pandasDFTransforms):
            try:
                pandasDF = pandasDFTransform(pandasDF)

            except Exception as err:
                self.stdout_logger.error(
                    msg='*** "{}": PANDAS TRANSFORM #{} ***'
                        .format(pieceSubPath, i))
                raise err

        return pandasDF

    def filterByPartitionKeys(self, *filterCriteriaTuples, **kwargs):
        filterCriteria = {}
        _samplePieceSubPath = next(iter(self.pieceSubPaths))
        
        for filterCriteriaTuple in filterCriteriaTuples:
            assert isinstance(filterCriteriaTuple, (list, tuple))
            filterCriteriaTupleLen = len(filterCriteriaTuple)

            col = filterCriteriaTuple[0]

            if '{}='.format(col) in _samplePieceSubPath:
                if filterCriteriaTupleLen == 2:
                    fromVal = toVal = None
                    inSet = {str(v) for v in to_iterable(filterCriteriaTuple[1])}

                elif filterCriteriaTupleLen == 3:
                    fromVal = filterCriteriaTuple[1]
                    if fromVal is not None:
                        fromVal = str(fromVal)

                    toVal = filterCriteriaTuple[2]
                    if toVal is not None:
                        toVal = str(toVal)

                    inSet = None

                else:
                    raise ValueError(
                        '*** {} FILTER CRITERIA MUST BE EITHER (<colName>, <fromVal>, <toVal>) OR (<colName>, <inValsSet>) ***'
                            .format(type(self)))

                filterCriteria[col] = fromVal, toVal, inSet

        if filterCriteria:
            pieceSubPaths = set()

            for pieceSubPath in self.pieceSubPaths:
                chk = True

                for col, (fromVal, toVal, inSet) in filterCriteria.items():
                    v = re.search('{}=(.*?)/'.format(col), pieceSubPath).group(1)

                    if ((fromVal is not None) and (v < fromVal)) or \
                            ((toVal is not None) and (v > toVal)) or \
                            ((inSet is not None) and (v not in inSet)):
                        chk = False
                        break

                if chk:
                    pieceSubPaths.add(pieceSubPath)

            assert pieceSubPaths, \
                '*** {}: NO PIECE PATHS SATISFYING FILTER CRITERIA {} ***'.format(self, filterCriteria)

            if arimo.debug.ON:
                self.stdout_logger.debug(
                    msg='*** {} PIECES SATISFYING FILTERING CRITERIA: {} ***'
                        .format(len(pieceSubPaths), filterCriteria))

            return self._subset(*pieceSubPaths, **kwargs)

        else:
            return self

    def sample(self, *args, **kwargs):
        stdKwArgs = self._extractStdKwArgs(kwargs, resetToClassDefaults=False, inplace=False)

        if stdKwArgs.alias and (stdKwArgs.alias == self.alias):
            stdKwArgs.alias = None

        stdKwArgs.detPrePartitioned = False
        stdKwArgs.nDetPrePartitions = None

        n = kwargs.pop('n', 1)
        minNPieces = kwargs.pop('minNPieces', self._reprSampleNPieces)
        maxNPieces = kwargs.pop('maxNPieces', None)
        verbose = kwargs.pop('verbose', True)

        sampleNPieces = \
            max(int(math.ceil(((min(n, self.nRows) / self.nRows) ** .5)
                              * self.nPieces)),
                minNPieces)

        if maxNPieces:
            sampleNPieces = min(sampleNPieces, maxNPieces)

        samplePieceSubPaths = \
            random.sample(
                population=self.pieceSubPaths,
                k=sampleNPieces) \
            if sampleNPieces < self.nPieces \
            else self.pieceSubPaths

        adfs = [super(ArrowSparkADF, self._pieceADF(samplePieceSubPath))
                    .sample(n=max(n / sampleNPieces, 1), *args, **kwargs)
                for samplePieceSubPath in
                    (tqdm.tqdm(samplePieceSubPaths)
                     if verbose
                     else samplePieceSubPaths)]

        adf = SparkADF.unionAllCols(*adfs, **stdKwArgs.__dict__)

        adf._cache.colWidth.update(adfs[0]._cache.colWidth)

        return adf

    def gen(self, *args, **kwargs):
        return _ArrowSparkADF__gen(
                args=args,
                path=self.path,
                pieceSubPaths=kwargs.get('pieceSubPaths', self.pieceSubPaths),
                aws_access_key_id=self._srcArrowDS.fs.fs.key, aws_secret_access_key=self._srcArrowDS.fs.fs.secret,
                iCol=self._iCol, tCol=self._tCol,
                possibleFeatureTAuxCols=self.possibleFeatureTAuxCols,
                contentCols=self.contentCols,
                pandasDFTransforms=self._pandasDFTransforms,
                filterConditions=kwargs.get('filter', {}),
                n=kwargs.get('n', 512),
                sampleN=kwargs.get('sampleN', 10 ** 5),
                anon=kwargs.get('anon', True),
                n_threads=kwargs.get('n_threads', 1))

    # ***********
    # REPR SAMPLE
    # _assignReprSample

    def _assignReprSample(self):
        adf = self.sample(
                n=self._reprSampleSize,
                minNPieces=self._reprSampleNPieces,
                anon=True) \
            .repartition(
                1,
                alias=(self.alias + self._REPR_SAMPLE_ALIAS_SUFFIX)
                    if self.alias
                    else None)

        adf.cache(
            eager=True,
            verbose=True)

        self._reprSampleSize = adf.nRows

        self._cache.reprSample = adf

        self._cache.nonNullProportion = {}
        self._cache.suffNonNull = {}

    # ****
    # MISC
    # split
    # copyToPath

    def split(self, *weights):
        if (not weights) or weights == (1,):
            return self

        else:
            nWeights = len(weights)
            cumuWeights = numpy.cumsum(weights) / sum(weights)

            nPieces = self.nPieces

            pieceSubPaths = list(self.pieceSubPaths)
            random.shuffle(pieceSubPaths)

            cumuIndices = \
                [0] + \
                [int(round(cumuWeights[i] * nPieces))
                 for i in range(nWeights)]

            return [self._subset(*pieceSubPaths[cumuIndices[i]:cumuIndices[i + 1]])
                    for i in range(nWeights)]

    def copyToPath(self, path, verbose=True):
        assert path.startswith('s3://')

        s3.sync(
            from_dir_path=self.path,
            to_dir_path=path,
            access_key_id=self._srcArrowDS.fs.fs.key,
            secret_access_key=self._srcArrowDS.fs.fs.secret,
            delete=True, quiet=True,
            verbose=verbose)
