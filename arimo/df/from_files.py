from __future__ import division, print_function

import abc
from argparse import Namespace as _Namespace
import datetime
import json
import math
import multiprocessing
import numpy
import os
import pandas
import psutil
import random
import re
import time
import tqdm

import six

if six.PY2:
    from functools32 import lru_cache
    from urlparse import urlparse
    _NUM_CLASSES = int, long, float
    _STR_CLASSES = str, unicode

else:
    from functools import lru_cache
    from urllib.parse import urlparse
    _NUM_CLASSES = int, float
    _STR_CLASSES = str

from pyarrow.filesystem import LocalFileSystem
from pyarrow.hdfs import HadoopFileSystem
from pyarrow.parquet import ParquetDataset, read_metadata, read_schema, read_table
from s3fs import S3FileSystem

from arimo.util import DefaultDict, fs, Namespace
from arimo.util.aws import s3
from arimo.util.date_time import gen_aux_cols, DATE_COL
from arimo.util.decor import enable_inplace, _docstr_settable_property, _docstr_verbose
from arimo.util.iterables import to_iterable
from arimo.util.types.arrow import \
    _ARROW_INT_TYPE, _ARROW_DOUBLE_TYPE, _ARROW_STR_TYPE, _ARROW_DATE_TYPE, \
    is_boolean, is_complex, is_float, is_integer, is_num, is_possible_cat, is_string
from arimo.util.types.numpy_pandas import NUMPY_FLOAT_TYPES, NUMPY_INT_TYPES, PY_NUM_TYPES
from arimo.util.types.spark_sql import _STR_TYPE
import arimo.debug

from . import _ADFABC
from .spark import SparkADF


class _ArrowADFABC(_ADFABC):
    __metaclass__ = abc.ABCMeta

    _REPR_SAMPLE_MIN_N_PIECES = 100

    # file systems
    _LOCAL_ARROW_FS = LocalFileSystem()

    _HDFS_ARROW_FS = \
        HadoopFileSystem(
            host='default',
            port=0,
            user=None,
            kerb_ticket=None,
            driver='libhdfs') \
        if fs._ON_LINUX_CLUSTER_WITH_HDFS \
        else None

    _S3_FSs = {}

    @classmethod
    def _s3FS(cls, key=None, secret=None):
        keyPair = key, secret
        if keyPair not in cls._S3_FSs:
            cls._S3_FSs[keyPair] = \
                S3FileSystem(
                    key=key,
                    secret=secret)
        return cls._S3_FSs[keyPair]

    @property
    def reprSampleMinNPieces(self):
        return self._reprSampleMinNPieces

    @reprSampleMinNPieces.setter
    def reprSampleMinNPieces(self, reprSampleMinNPieces):
        if (reprSampleMinNPieces <= self.nPieces) and (reprSampleMinNPieces != self._reprSampleMinNPieces):
            self._reprSampleMinNPieces = reprSampleMinNPieces

    @reprSampleMinNPieces.deleter
    def reprSampleMinNPieces(self):
        self._reprSampleMinNPieces = min(self._REPR_SAMPLE_MIN_N_PIECES, self.nPieces)


# class with __call__ to serve as pickle-able function for use in multi-processing
# ref: https://stackoverflow.com/questions/1947904/how-can-i-pickle-a-nested-class-in-python
class _ArrowADF__getattr__pandasDFTransform:
    def __init__(self, col):
        self.col = col

    def __call__(self, pandasDF):
        return getattr(pandasDF, self.col)


# class with __call__ to serve as pickle-able function for use in multi-processing
# ref: https://stackoverflow.com/questions/1947904/how-can-i-pickle-a-nested-class-in-python
class _ArrowADF__getitem__pandasDFTransform:
    def __init__(self, item):
        self.item = item

    def __call__(self, pandasDF):
        return pandasDF[self.item]


# class with __call__ to serve as pickle-able function for use in multi-processing
# ref: https://stackoverflow.com/questions/1947904/how-can-i-pickle-a-nested-class-in-python
class _ArrowADF__drop__pandasDFTransform:
    def __init__(self, cols):
        self.cols = list(cols)

    def __call__(self, pandasDF):
        return pandasDF.drop(
            columns=self.cols,
            level=None,
            inplace=False,
            errors='ignore')


# class with __call__ to serve as pickle-able function for use in multi-processing
# ref: https://stackoverflow.com/questions/1947904/how-can-i-pickle-a-nested-class-in-python
class _ArrowADF__castType__pandasDFTransform:
    def __init__(self, col, asType, asCol=None):
        self.col = col
        self.asType = asType
        self.asCol = asCol

    def __call__(self, pandasDF):
        series = \
            pandasDF[self.col].astype(
                dtype=self.asType,
                copy=False,
                errors='raise')

        if self.asCol:
            pandasDF[self.asCol] = series

        else:
            pandasDF[self.col] = series

        return pandasDF


# class with __call__ to serve as pickle-able function for use in multi-processing
# ref: https://stackoverflow.com/questions/1947904/how-can-i-pickle-a-nested-class-in-python
class _ArrowADF__encodeStr__pandasDFTransform:
    def __init__(self, col, strs, asCol=None):
        self.col = col
        self.strs = strs
        self.asCol = asCol

    def __call__(self, pandasDF):
        series = \
            pandasDF[self.col] \
            .loc[pandas.notnull(pandasDF[self.col])] \
            .map(lambda x: self.strs.index(x))

        if self.asCol:
            pandasDF[self.asCol] = series

        else:
            pandasDF[self.col] = series

        return pandasDF


# class with __call__ to serve as pickle-able function for use in multi-processing
# ref: https://stackoverflow.com/questions/1947904/how-can-i-pickle-a-nested-class-in-python
class _ArrowADF__fillna__pandasDFTransform:
    def __init__(self, nullFillDetails, _medianFill=False):
        self.nullFillDetails = nullFillDetails

        self._medianFill = _medianFill
        if _medianFill and arimo.debug.ON:
            print('*** Median-NULL-Filling ACTIVATED ***')

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

                nullFillValue = \
                    series.loc[chks] \
                        .median(
                            axis='index',
                            skipna=True,
                            level=None) \
                    if self._medianFill \
                    else None

                if pandas.isnull(nullFillValue):
                    nullFillValue = nullFill['NullFillValue']

                pandasDF[_ADFABC._NULL_FILL_PREFIX + col + _ADFABC._PREP_SUFFIX] = \
                    series.where(
                        cond=chks,
                        other=nullFillValue,
                        inplace=False,
                        axis=None,
                        level=None,
                        errors='raise',
                        try_cast=False)

        return pandasDF


# class with __call__ to serve as pickle-able function for use in multi-processing
# ref: https://stackoverflow.com/questions/1947904/how-can-i-pickle-a-nested-class-in-python
class _ArrowADF__prep__pandasDFTransform:
    def __init__(self, addCols, typeStrs, catOrigToPrepColMap, numOrigToPrepColMap, _medianFill=False):
        self.addCols = addCols

        self.typeStrs = typeStrs

        assert not catOrigToPrepColMap['__OHE__']
        self.catOrigToPrepColMap = catOrigToPrepColMap
        self.scaleCat = catOrigToPrepColMap['__SCALE__']

        self.numOrigToPrepColMap = numOrigToPrepColMap
        self.numScaler = numOrigToPrepColMap['__SCALER__']
        assert self.numScaler in ('standard', 'maxabs', 'minmax', None)

        self._medianFill = _medianFill
        if _medianFill and arimo.debug.ON:
            print('*** Median-NULL-Filling ACTIVATED ***')

    def __call__(self, pandasDF):
        _FLOAT_ABS_TOL = 1e-6

        for col, value in self.addCols.items():
            pandasDF[col] = value

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
                    if self.typeStrs[catCol] == _STR_TYPE \
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

        pandasDF = \
            _ArrowADF__fillna__pandasDFTransform(
                nullFillDetails=self.numOrigToPrepColMap,
                _medianFill=self._medianFill)(
                pandasDF=pandasDF)

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


_PIECE_LOCAL_CACHE_PATHS = {}


# class with __call__ to serve as pickle-able function for use in multi-processing
# ref: https://stackoverflow.com/questions/1947904/how-can-i-pickle-a-nested-class-in-python
class _ArrowADF__pieceArrowTableFunc:
    def __init__(self, aws_access_key_id=None, aws_secret_access_key=None, n_threads=1):
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key

        self.n_threads = n_threads

    def __call__(self, piecePath):
        if piecePath.startswith('s3'):
            global _PIECE_LOCAL_CACHE_PATHS

            if piecePath in _PIECE_LOCAL_CACHE_PATHS:
                path = _PIECE_LOCAL_CACHE_PATHS[piecePath]

            else:
                parsedURL = \
                    urlparse(
                        url=piecePath,
                        scheme='',
                        allow_fragments=True)

                _PIECE_LOCAL_CACHE_PATHS[piecePath] = path = \
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

        else:
            path = piecePath

        return read_table(
                source=path,
                columns=None,
                nthreads=self.n_threads,
                metadata=None,
                use_pandas_metadata=False)


# class with __call__ to serve as pickle-able function for use in multi-processing
# ref: https://stackoverflow.com/questions/1947904/how-can-i-pickle-a-nested-class-in-python
class _ArrowADF__gen:
    def __init__(
            self, args,
            piecePaths,
            aws_access_key_id, aws_secret_access_key,
            partitionKVs,
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

        self.piecePaths = list(piecePaths)

        self.partitionKVs = partitionKVs

        self.n_threads = n_threads

        self.pieceArrowTableFunc = \
            _ArrowADF__pieceArrowTableFunc(
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
            piecePath = random.choice(self.piecePaths)

            chunkPandasDF = \
                random.choice(
                    self.pieceArrowTableFunc(piecePath=piecePath)
                        .to_batches(chunksize=self.sampleN)) \
                    .to_pandas(
                        nthreads=self.n_threads)

            if self.partitionKVs:
                for k, v in self.partitionKVs[piecePath].items():
                    chunkPandasDF[k] = v

            else:
                for partitionKV in re.findall('[^/]+=[^/]+/', piecePath):
                    k, v = partitionKV.split('=')
                    k = str(k)   # ensure not Unicode

                    chunkPandasDF[k] = \
                        datetime.datetime.strptime(v[:-1], '%Y-%m-%d').date() \
                        if k == DATE_COL \
                        else v[:-1]

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
                    print('*** "{}": PANDAS TRANSFORM #{} ***'.format(piecePath, i))
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

                arrays = \
                    [(numpy.vstack(
                            numpy.expand_dims(
                                chunkPandasDF.loc[(rowIdx + rowFrom_n_rowTo[0]):(rowIdx + rowFrom_n_rowTo[1] + 1), cols].values,
                                axis=0)
                            for rowIdx in rowIndicesSubset)
                        if overTime
                        else chunkPandasDF.loc[rowIndicesSubset, cols].values)
                       for cols, overTime, rowFrom_n_rowTo in
                        zip(self.colsLists, self.colsOverTime, self.rowFrom_n_rowTo_tups)]

                if arimo.debug.ON:
                    for array in arrays:
                        nNaNs = numpy.isnan(array).sum()
                        assert not nNaNs, '*** {}: {} NaNs ***'.format(array.shape, nNaNs)

                yield arrays


@enable_inplace
class ArrowADF(_ArrowADFABC):
    _CACHE = {}
    
    _PIECE_CACHES = {}

    _T_AUX_COL_ARROW_TYPES = {
        _ArrowADFABC._T_ORD_COL: _ARROW_INT_TYPE,
        _ArrowADFABC._T_DELTA_COL: _ARROW_DOUBLE_TYPE,

        _ArrowADFABC._T_HoY_COL: _ARROW_INT_TYPE,   # Half of Year
        _ArrowADFABC._T_QoY_COL: _ARROW_INT_TYPE,   # Quarter of Year
        _ArrowADFABC._T_MoY_COL: _ARROW_INT_TYPE,   # Month of Year
        # _ArrowADFABC._T_WoY_COL: _ARROW_INT_TYPE,   # Week of Year
        # _ArrowADFABC._T_DoY_COL: _ARROW_INT_TYPE,   # Day of Year
        _ArrowADFABC._T_PoY_COL: _ARROW_DOUBLE_TYPE,   # Part/Proportion/Fraction of Year

        _ArrowADFABC._T_QoH_COL: _ARROW_INT_TYPE,   # Quarter of Half-Year
        _ArrowADFABC._T_MoH_COL: _ARROW_INT_TYPE,   # Month of Half-Year
        _ArrowADFABC._T_PoH_COL: _ARROW_DOUBLE_TYPE,   # Part/Proportion/Fraction of Half-Year

        _ArrowADFABC._T_MoQ_COL: _ARROW_INT_TYPE,   # Month of Quarter
        _ArrowADFABC._T_PoQ_COL: _ARROW_DOUBLE_TYPE,   # Part/Proportion/Fraction of Quarter

        _ArrowADFABC._T_WoM_COL: _ARROW_INT_TYPE,   # Week of Month
        _ArrowADFABC._T_DoM_COL: _ARROW_INT_TYPE,   # Day of Month
        _ArrowADFABC._T_PoM_COL: _ARROW_DOUBLE_TYPE,   # Part/Proportion/Fraction of Month

        _ArrowADFABC._T_DoW_COL: _ARROW_INT_TYPE,   # Day of Week
        _ArrowADFABC._T_PoW_COL: _ARROW_DOUBLE_TYPE,   # Part/Proportion/Fraction of Week

        _ArrowADFABC._T_HoD_COL: _ARROW_INT_TYPE,   # Hour of Day
        _ArrowADFABC._T_PoD_COL: _ARROW_DOUBLE_TYPE   # Part/Proportion/Fraction of Day
    }

    # default arguments dict
    _DEFAULT_KWARGS = \
        dict(
            iCol=_ArrowADFABC._DEFAULT_I_COL, tCol=None,

            reprSampleMinNPieces=_ArrowADFABC._REPR_SAMPLE_MIN_N_PIECES,
            reprSampleSize=_ArrowADFABC._DEFAULT_REPR_SAMPLE_SIZE,

            minNonNullProportion=DefaultDict(_ArrowADFABC._DEFAULT_MIN_NON_NULL_PROPORTION),
            outlierTailProportion=DefaultDict(_ArrowADFABC._DEFAULT_OUTLIER_TAIL_PROPORTION),
            maxNCats=DefaultDict(_ArrowADFABC._DEFAULT_MAX_N_CATS),
            minProportionByMaxNCats=DefaultDict(_ArrowADFABC._DEFAULT_MIN_PROPORTION_BY_MAX_N_CATS))

    # "inplace-able" methods
    _INPLACE_ABLE = \
        'filter', \
        'map', \
        'rename', \
        '_subset', \
        'drop', \
        'fillna', \
        'filterByPartitionKeys', \
        'prep'

    # *****************
    # METHODS TO CREATE
    # __init__
    # load

    def __init__(
            self, path=None, reCache=False,
            aws_access_key_id=None, aws_secret_access_key=None,
            _mappers=[],
            verbose=True,
            **kwargs):
        if verbose or arimo.debug.ON:
            logger = self.class_stdout_logger()

        self.path = path

        _aPath = path \
            if isinstance(path, _STR_CLASSES) \
            else path[0]

        self.fromS3 = _aPath.startswith('s3://')
        self.fromHDFS = _aPath.startswith('hdfs:')

        if (not reCache) and (path in self._CACHE):
            _cache = self._CACHE[path]

        else:
            self._CACHE[path] = _cache = Namespace()

        if _cache:
            if arimo.debug.ON:
                logger.debug('*** RETRIEVING CACHE FOR "{}" ***'.format(path))

        else:
            if self.fromS3:
                self.s3Client = \
                    _cache.s3Client = \
                    s3.client(
                        access_key_id=aws_access_key_id,
                        secret_access_key=aws_secret_access_key)

                _parsedURL = urlparse(url=_aPath, scheme='', allow_fragments=True)
                _cache.s3Bucket = _parsedURL.netloc

                _cache.tmpDirS3Key = self._TMP_DIR_PATH.strip('/')

                _cache.tmpDirPath = \
                    os.path.join(
                        's3://{}'.format(_cache.s3Bucket),
                        _cache.tmpDirS3Key)

            else:
                _cache.s3Client = _cache.s3Bucket = _cache.tmpDirS3Key = None
                _cache.tmpDirPath = self._TMP_DIR_PATH

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
                        if self.fromS3
                        else (self._HDFS_ARROW_FS
                              if self.fromHDFS
                              else self._LOCAL_ARROW_FS),
                    schema=None, validate_schema=False, metadata=None,
                    split_row_groups=False)

            if verbose:
                toc = time.time()
                logger.info(msg + ' done!   <{:,.1f} s>'.format(toc - tic))

            _cache.nPieces = len(_cache._srcArrowDS.pieces)

            if _cache.nPieces:
                _cache.piecePaths = \
                    {piece.path
                     for piece in _cache._srcArrowDS.pieces}

            else:
                _cache.nPieces = 1
                _cache.piecePaths = {path}

            _cache.srcColsInclPartitionKVs = set()
            _cache.srcTypesInclPartitionKVs = Namespace()

            for i, piecePath in enumerate(_cache.piecePaths):
                if piecePath in self._PIECE_CACHES:
                    pieceCache = self._PIECE_CACHES[piecePath]

                    pieceCache.arrowDFs.add(self)

                else:
                    srcColsInclPartitionKVs = []

                    srcTypesExclPartitionKVs = Namespace()
                    srcTypesInclPartitionKVs = Namespace()

                    partitionKVs = {}

                    for partitionKV in re.findall('[^/]+=[^/]+/', piecePath):
                        k, v = partitionKV.split('=')
                        k = str(k)   # ensure not Unicode

                        srcColsInclPartitionKVs.append(k)

                        if k == DATE_COL:
                            srcTypesInclPartitionKVs[k] = _ARROW_DATE_TYPE
                            partitionKVs[k] = datetime.datetime.strptime(v[:-1], '%Y-%m-%d').date()

                        else:
                            srcTypesInclPartitionKVs[k] = _ARROW_STR_TYPE
                            partitionKVs[k] = v[:-1]

                    if i:
                        localOrHDFSPath = \
                            None \
                            if self.fromS3 \
                            else piecePath

                        srcColsExclPartitionKVs = None

                        nRows = None

                    else:
                        localOrHDFSPath = self.pieceLocalOrHDFSPath(piecePath=piecePath)

                        schema = read_schema(where=localOrHDFSPath)

                        srcColsExclPartitionKVs = schema.names

                        srcColsInclPartitionKVs += schema.names

                        for col in schema.names:
                            srcTypesExclPartitionKVs[col] = \
                                srcTypesInclPartitionKVs[col] = \
                                schema.field_by_name(col).type

                        nRows = read_metadata(where=localOrHDFSPath).num_rows

                    self._PIECE_CACHES[piecePath] = \
                        pieceCache = \
                        Namespace(
                            arrowDFs={self},

                            localOrHDFSPath=localOrHDFSPath,
                            partitionKVs=partitionKVs,

                            srcColsExclPartitionKVs=srcColsExclPartitionKVs,
                            srcColsInclPartitionKVs=srcColsInclPartitionKVs,

                            srcTypesExclPartitionKVs=srcTypesExclPartitionKVs,
                            srcTypesInclPartitionKVs=srcTypesInclPartitionKVs,
                            
                            nRows=nRows)

                _cache.srcColsInclPartitionKVs.update(pieceCache.srcColsInclPartitionKVs)

                for col, arrowType in pieceCache.srcTypesInclPartitionKVs.items():
                    if col in _cache.srcTypesInclPartitionKVs:
                        assert arrowType == _cache.srcTypesInclPartitionKVs[col], \
                            '*** {} COLUMN {}: DETECTED TYPE {} != {} ***'.format(
                                piecePath, col, arrowType, _cache.srcTypesInclPartitionKVs[col])

                    else:
                        _cache.srcTypesInclPartitionKVs[col] = arrowType

        self.__dict__.update(_cache)

        self._mappers = _mappers

        # extract standard keyword arguments
        self._extractStdKwArgs(kwargs, resetToClassDefaults=True, inplace=True)

        # organize time series if applicable
        self._organizeTimeSeries()

        # set profiling settings and create empty profiling cache
        self._emptyCache()

    @classmethod
    def load(cls, path, **kwargs):
        return cls(path=path, **kwargs)

    # ********************************
    # "INTERNAL / DON'T TOUCH" METHODS
    # _extractStdKwArgs
    # _organizeTimeSeries
    # _emptyCache
    # _inheritCache
    # _inplace

    def _extractStdKwArgs(self, kwargs, resetToClassDefaults=False, inplace=False):
        nameSpace = self \
            if inplace \
            else _Namespace()

        for k, classDefaultV in self._DEFAULT_KWARGS.items():
            _privateK = '_{}'.format(k)

            if not resetToClassDefaults:
                existingInstanceV = getattr(self, _privateK, None)

            v = kwargs.pop(
                k,
                existingInstanceV
                    if (not resetToClassDefaults) and existingInstanceV
                    else classDefaultV)

            if (k == 'reprSampleMinNPieces') and (v > self.nPieces):
                v = self.nPieces

            setattr(
                nameSpace,
                _privateK   # *** USE _k TO NOT INVOKE @k.setter RIGHT AWAY ***
                    if inplace
                    else k,
                v)

        if inplace:
            cols = self.srcColsInclPartitionKVs

            if self._iCol not in cols:
                self._iCol = None

            if self._tCol not in cols:
                self._tCol = None

        else:
            return nameSpace

    def _organizeTimeSeries(self):
        self._dCol = DATE_COL \
            if DATE_COL in self.srcColsInclPartitionKVs \
            else None

        self.hasTS = self._iCol and self._tCol

    def _emptyCache(self):
        self._cache = \
            _Namespace(
                prelimReprSamplePiecePaths=None,
                reprSamplePiecePaths=None,
                reprSample=None,

                nRows=None,
                approxNRows=None,

                count={}, distinct={},   # approx.

                nonNullProportion={},   # approx.
                suffNonNullProportionThreshold={},
                suffNonNull={},

                sampleMin={}, sampleMax={}, sampleMean={}, sampleMedian={},
                outlierRstMin={}, outlierRstMax={}, outlierRstMean={}, outlierRstMedian={},

                colWidth={})

    def _inheritCache(self, arrowDF, *sameCols, **newColToOldColMappings):
        if arrowDF._cache.nRows:
            if self._cache.nRows is None:
                self._cache.nRows = arrowDF._cache.nRows
            else:
                assert self._cache.nRows == arrowDF._cache.nRows

        if arrowDF._cache.approxNRows and (self._cache.approxNRows is None):
            self._cache.approxNRows = arrowDF._cache.approxNRows

        commonCols = set(self.columns).intersection(arrowDF.columns)

        if sameCols or newColToOldColMappings:
            for newCol, oldCol in newColToOldColMappings.items():
                assert newCol in self.columns
                assert oldCol in arrowDF.columns

            for sameCol in commonCols.difference(newColToOldColMappings).intersection(sameCols):
                newColToOldColMappings[sameCol] = sameCol

        else:
            newColToOldColMappings = \
                {col: col
                 for col in commonCols}

        for cacheCategory in \
                ('count', 'distinct',
                 'nonNullProportion', 'suffNonNullProportionThreshold', 'suffNonNull',
                 'sampleMin', 'sampleMax', 'sampleMean', 'sampleMedian',
                 'outlierRstMin', 'outlierRstMax', 'outlierRstMean', 'outlierRstMedian',
                 'colWidth'):
            for newCol, oldCol in newColToOldColMappings.items():
                if oldCol in arrowDF._cache.__dict__[cacheCategory]:
                    self._cache.__dict__[cacheCategory][newCol] = \
                        arrowDF._cache.__dict__[cacheCategory][oldCol]

    def _inplace(self, arrowADF):
        if isinstance(arrowADF, (tuple, list)):   # just in case we're taking in multiple inputs
            arrowADF = arrowADF[0]

        assert isinstance(arrowADF, ArrowADF)

        self.path = arrowADF.path

        self.__dict__.update(self._CACHE[arrowADF.path])

        self._mappers = arrowADF._mappers

        self._cache = arrowADF._cache

    # **********
    # IO METHODS
    # save

    def save(self, *args, **kwargs):
        return NotImplemented

    def copy(self, **kwargs):
        resetMappers = kwargs.pop('resetMappers')

        if self.fromS3:
            aws_access_key_id = self._srcArrowDS.fs.fs.key
            aws_secret_access_key = self._srcArrowDS.fs.fs.secret

        else:
            aws_access_key_id = aws_secret_access_key = None

        return ArrowADF(
                path=self.path,
                aws_access_key_id=aws_access_key_id, aws_secret_access_key=aws_secret_access_key,

                iCol=self._iCol, tCol=self._tCol,

                _mappers=[]
                    if resetMappers
                    else self._mappers,

                reprSampleMinNPieces=self._reprSampleMinNPieces,
                reprSampleSize=self._reprSampleSize,

                minNonNullProportion=self._minNonNullProportion,
                outlierTailProportion=self._outlierTailProportion,
                maxNCats=self._maxNCats,
                minProportionByMaxNCats=self._minProportionByMaxNCats,

                **kwargs)

    # ***************
    # PYTHON STR/REPR
    # __repr__
    # __short_repr__

    @property
    def _pathRepr(self):
        return '"{}"'.format(self.path) \
            if isinstance(self.path, _STR_CLASSES) \
          else '{} Paths e.g. {}'.format(len(self.path), self.path[:3])

    def __repr__(self):
        cols_and_types_str = []

        if self._iCol:
            cols_and_types_str += ['(iCol) {}: {}'.format(self._iCol, self.type(self._iCol))]

        if self._dCol:
            cols_and_types_str += ['(dCol) {}: {}'.format(self._dCol, self.type(self._dCol))]

        if self._tCol:
            cols_and_types_str += ['(tCol) {}: {}'.format(self._tCol, self.type(self._tCol))]

        cols_and_types_str += \
            ['{}: {}'.format(col, self.type(col))
             for col in self.contentCols]
        
        return '{:,}-piece {}{}[{} + {:,} transform(s)][{}]'.format(
            self.nPieces,
            '{:,}-row '.format(self._cache.nRows)
                if self._cache.nRows
                else ('approx-{:,.0f}-row '.format(self._cache.approxNRows)
                      if self._cache.approxNRows
                      else ''),
            type(self).__name__,
            self._pathRepr,
            len(self._mappers),
            ', '.join(cols_and_types_str))

    @property
    def __short_repr__(self):
        cols_desc_str = []

        if self._iCol:
            cols_desc_str += ['iCol: {}'.format(self._iCol)]

        if self._dCol:
            cols_desc_str += ['dCol: {}'.format(self._dCol)]

        if self._tCol:
            cols_desc_str += ['tCol: {}'.format(self._tCol)]

        cols_desc_str += ['{} content col(s)'.format(len(self.contentCols))]

        return '{:,}-piece {}{}[{} + {:,} transform(s)][{}]'.format(
            self.nPieces,
            '{:,}-row '.format(self._cache.nRows)
                if self._cache.nRows
                else ('approx-{:,.0f}-row '.format(self._cache.approxNRows)
                      if self._cache.approxNRows
                      else ''),
            type(self).__name__,
            self._pathRepr,
            len(self._mappers),
            ', '.join(cols_desc_str))

    # ***************
    # CACHING METHODS
    # pieceLocalOrHDFSPath

    def pieceLocalOrHDFSPath(self, piecePath):
        if (piecePath in self._PIECE_CACHES) and self._PIECE_CACHES[piecePath].localOrHDFSPath:
            return self._PIECE_CACHES[piecePath].localOrHDFSPath

        else:
            if self.fromS3:
                parsedURL = \
                    urlparse(
                        url=piecePath,
                        scheme='',
                        allow_fragments=True)

                localOrHDFSPath = \
                    os.path.join(
                        self._TMP_DIR_PATH,
                        parsedURL.netloc,
                        parsedURL.path[1:])

                fs.mkdir(
                    dir=os.path.dirname(localOrHDFSPath),
                    hdfs=False)

                self.s3Client.download_file(
                    Bucket=parsedURL.netloc,
                    Key=parsedURL.path[1:],
                    Filename=localOrHDFSPath)

            else:
                localOrHDFSPath = piecePath

            if piecePath in self._PIECE_CACHES:
                self._PIECE_CACHES[piecePath].localOrHDFSPath = localOrHDFSPath

            return localOrHDFSPath

    # ***********************
    # MAP-REDUCE (PARTITIONS)
    # map
    # reduce
    # __getattr__
    # __getitem__
    # drop
    # rename
    # filter
    # collect
    # toPandas

    def map(self, mapper=[], **kwargs):
        inheritCache = kwargs.pop('inheritCache', False)
        inheritNRows = kwargs.pop('inheritNRows', inheritCache)

        additionalMappers = \
            mapper \
                if isinstance(mapper, list) \
                else [mapper]

        if self.fromS3:
            aws_access_key_id = self._srcArrowDS.fs.fs.key
            aws_secret_access_key = self._srcArrowDS.fs.fs.secret

        else:
            aws_access_key_id = aws_secret_access_key = None

        arrowADF = \
            ArrowADF(
                path=self.path,
                aws_access_key_id=aws_access_key_id, aws_secret_access_key=aws_secret_access_key,

                iCol=self._iCol, tCol=self._tCol,
                _mappers=self._mappers + additionalMappers,

                reprSampleMinNPieces=self._reprSampleMinNPieces,
                reprSampleSize=self._reprSampleSize,

                minNonNullProportion=self._minNonNullProportion,
                outlierTailProportion=self._outlierTailProportion,
                maxNCats=self._maxNCats,
                minProportionByMaxNCats=self._minProportionByMaxNCats,

                **kwargs)

        if inheritCache:
            arrowADF._inheritCache(self)

        return arrowADF

    def reduce(self, *piecePaths, **kwargs):
        _CHUNK_SIZE = 10 ** 5

        nSamplesPerPiece = kwargs.get('nSamplesPerPiece')

        genTAuxCols = kwargs.get('genTAuxCols', True)

        reducer = \
            kwargs.get(
                'reducer',
                lambda results:
                    pandas.concat(
                        objs=results,
                        axis='index',
                        join='outer',
                        join_axes=None,
                        ignore_index=True,
                        keys=None,
                        levels=None,
                        names=None,
                        verify_integrity=False,
                        copy=False))

        verbose = kwargs.pop('verbose', True)
        
        if not piecePaths:
            piecePaths = self.piecePaths

        results = []

        for piecePath in \
                (tqdm.tqdm(piecePaths)
                 if verbose
                 else piecePaths):
            pieceLocalOrHDFSPath = self.pieceLocalOrHDFSPath(piecePath=piecePath)

            pieceCache = self._PIECE_CACHES[piecePath]

            if pieceCache.nRows is None:
                schema = read_schema(where=pieceLocalOrHDFSPath)

                pieceCache.srcColsExclPartitionKVs = schema.names

                pieceCache.srcColsInclPartitionKVs += schema.names
                
                self.srcColsInclPartitionKVs.update(schema.names)

                for col in schema.names:
                    pieceCache.srcTypesExclPartitionKVs[col] = \
                        pieceCache.srcTypesInclPartitionKVs[col] = \
                        _arrowType = \
                        schema.field_by_name(col).type

                    if col in self.srcTypesInclPartitionKVs:
                        assert _arrowType == self.srcTypesInclPartitionKVs[col], \
                            '*** {} COLUMN {}: DETECTED TYPE {} != {} ***'.format(
                                piecePath, col, _arrowType, self.srcTypesInclPartitionKVs[col])

                    else:
                        self.srcTypesInclPartitionKVs[col] = _arrowType

                pieceCache.nRows = read_metadata(where=pieceLocalOrHDFSPath).num_rows

            cols = kwargs.get('cols')

            cols = to_iterable(cols, iterable_type=set) \
                if cols \
                else set(pieceCache.srcColsInclPartitionKVs)

            srcCols = cols.intersection(pieceCache.srcColsExclPartitionKVs)
            
            partitionKeyCols = cols.intersection(pieceCache.partitionKVs)

            if srcCols:
                pieceArrowTable = \
                    read_table(
                        source=pieceLocalOrHDFSPath,
                        columns=srcCols,
                        nthreads=psutil.cpu_count(logical=True),
                        metadata=None,
                        use_pandas_metadata=False)

                if nSamplesPerPiece and (nSamplesPerPiece < pieceCache.nRows):
                    intermediateN = (nSamplesPerPiece * pieceCache.nRows) ** .5

                    nChunks = int(math.ceil(pieceCache.nRows / _CHUNK_SIZE))
                    nChunksForIntermediateN = int(math.ceil(intermediateN / _CHUNK_SIZE))

                    nSamplesPerChunk = int(math.ceil(nSamplesPerPiece / nChunksForIntermediateN))

                    if nChunksForIntermediateN < nChunks:
                        recordBatches = pieceArrowTable.to_batches(chunksize=_CHUNK_SIZE)

                        nRecordBatches = len(recordBatches)

                        assert nRecordBatches in (nChunks - 1, nChunks), \
                            '*** {}: {} vs. {} Record Batches ***'.format(piecePath, nRecordBatches, nChunks)

                        assert nChunksForIntermediateN <= nRecordBatches, \
                            '*** {}: {} vs. {} Record Batches ***'.format(piecePath, nChunksForIntermediateN, nRecordBatches)

                        chunkPandasDFs = []

                        for recordBatch in \
                                random.sample(
                                    population=recordBatches,
                                    k=nChunksForIntermediateN):
                            chunkPandasDF = recordBatch.to_pandas(nthreads=max(1, psutil.cpu_count() // 2))

                            for k in partitionKeyCols:
                                chunkPandasDF[k] = pieceCache.partitionKVs[k]

                            if genTAuxCols and (self._tCol in chunkPandasDF.columns):
                                if self._iCol in chunkPandasDF.columns:
                                    try:
                                        chunkPandasDF = \
                                            gen_aux_cols(
                                                df=chunkPandasDF.loc[
                                                    pandas.notnull(chunkPandasDF[self._iCol]) &
                                                    pandas.notnull(chunkPandasDF[self._tCol])],
                                                i_col=self._iCol, t_col=self._tCol)

                                    except Exception as err:
                                        print('*** {} ***'.format(piecePath))
                                        raise err

                                else:
                                    try:
                                        chunkPandasDF = \
                                            gen_aux_cols(
                                                df=chunkPandasDF.loc[pandas.notnull(chunkPandasDF[self._tCol])],
                                                i_col=None, t_col=self._tCol)

                                    except Exception as err:
                                        print('*** {} ***'.format(piecePath))
                                        raise err

                            if nSamplesPerChunk < len(chunkPandasDF):
                                chunkPandasDF = \
                                    chunkPandasDF.sample(
                                        n=nSamplesPerChunk,
                                            # Number of items from axis to return.
                                            # Cannot be used with frac.
                                            # Default = 1 if frac = None.
                                            # frac=None,
                                            # Fraction of axis items to return.
                                            # Cannot be used with n.
                                        replace=False,
                                            # Sample with or without replacement.
                                            # Default = False.
                                        weights=None,
                                            # Default None results in equal probability weighting.
                                            # If passed a Series, will align with target object on index.
                                            # Index values in weights not found in sampled object will be ignored
                                            # and index values in sampled object not in weights will be assigned weights of zero.
                                            # If called on a DataFrame, will accept the name of a column when axis = 0.
                                            # Unless weights are a Series, weights must be same length as axis being sampled.
                                            # If weights do not sum to 1, they will be normalized to sum to 1.
                                            # Missing values in the weights column will be treated as zero.
                                            # inf and -inf values not allowed.
                                        random_state=None,
                                            # Seed for the random number generator (if int), or numpy RandomState object.
                                        axis='index')

                            chunkPandasDFs.append(chunkPandasDF)

                        piecePandasDF = \
                            pandas.concat(
                                objs=chunkPandasDFs,
                                axis='index',
                                join='outer',
                                join_axes=None,
                                ignore_index=True,
                                keys=None,
                                levels=None,
                                names=None,
                                verify_integrity=False,
                                copy=False)

                    else:
                        piecePandasDF = \
                            pieceArrowTable.to_pandas(
                                nthreads=max(1, psutil.cpu_count(logical=True) // 2),
                                    # For the default, we divide the CPU count by 2
                                    # because most modern computers have hyperthreading turned on,
                                    # so doubling the CPU count beyond the number of physical cores does not help
                                strings_to_categorical=False,
                                memory_pool=None,
                                zero_copy_only=None,
                                categories=[],
                                integer_object_nulls=False)

                        for k in partitionKeyCols:
                            piecePandasDF[k] = pieceCache.partitionKVs[k]

                        if genTAuxCols and (self._tCol in piecePandasDF.columns):
                            if self._iCol in piecePandasDF.columns:
                                try:
                                    piecePandasDF = \
                                        gen_aux_cols(
                                            df=piecePandasDF.loc[
                                                pandas.notnull(piecePandasDF[self._iCol]) &
                                                pandas.notnull(piecePandasDF[self._tCol])],
                                            i_col=self._iCol, t_col=self._tCol)

                                except Exception as err:
                                    print('*** {} ***'.format(piecePath))
                                    raise err

                            else:
                                try:
                                    piecePandasDF = \
                                        gen_aux_cols(
                                            df=piecePandasDF.loc[pandas.notnull(piecePandasDF[self._tCol])],
                                            i_col=None, t_col=self._tCol)

                                except Exception as err:
                                    print('*** {} ***'.format(piecePath))
                                    raise err

                        piecePandasDF = \
                            piecePandasDF.sample(
                                n=nSamplesPerPiece,
                                    # Number of items from axis to return.
                                    # Cannot be used with frac.
                                    # Default = 1 if frac = None.
                                # frac=None,
                                    # Fraction of axis items to return.
                                    # Cannot be used with n.
                                replace=False,
                                    # Sample with or without replacement.
                                    # Default = False.
                                weights=None,
                                    # Default None results in equal probability weighting.
                                    # If passed a Series, will align with target object on index.
                                    # Index values in weights not found in sampled object will be ignored
                                    # and index values in sampled object not in weights will be assigned weights of zero.
                                    # If called on a DataFrame, will accept the name of a column when axis = 0.
                                    # Unless weights are a Series, weights must be same length as axis being sampled.
                                    # If weights do not sum to 1, they will be normalized to sum to 1.
                                    # Missing values in the weights column will be treated as zero.
                                    # inf and -inf values not allowed.
                                random_state=None,
                                    # Seed for the random number generator (if int), or numpy RandomState object.
                                axis='index')

                else:
                    piecePandasDF = \
                        pieceArrowTable.to_pandas(
                            nthreads=max(1, psutil.cpu_count(logical=True) // 2),
                                # For the default, we divide the CPU count by 2
                                # because most modern computers have hyperthreading turned on,
                                # so doubling the CPU count beyond the number of physical cores does not help
                            strings_to_categorical=False,
                            memory_pool=None,
                            zero_copy_only=None,
                            categories=[],
                            integer_object_nulls=False)

                    for k in partitionKeyCols:
                        piecePandasDF[k] = pieceCache.partitionKVs[k]

                    if genTAuxCols and (self._tCol in piecePandasDF.columns):
                        if self._iCol in piecePandasDF.columns:
                            try:
                                piecePandasDF = \
                                    gen_aux_cols(
                                        df=piecePandasDF.loc[
                                            pandas.notnull(piecePandasDF[self._iCol]) &
                                            pandas.notnull(piecePandasDF[self._tCol])],
                                        i_col=self._iCol, t_col=self._tCol)

                            except Exception as err:
                                print('*** {} ***'.format(piecePath))
                                raise err

                        else:
                            try:
                                piecePandasDF = \
                                    gen_aux_cols(
                                        df=piecePandasDF.loc[pandas.notnull(piecePandasDF[self._tCol])],
                                        i_col=None, t_col=self._tCol)

                            except Exception as err:
                                print('*** {} ***'.format(piecePath))
                                raise err

            else:
                piecePandasDF = pandas.DataFrame(
                    index=range(nSamplesPerPiece
                                if nSamplesPerPiece and (nSamplesPerPiece < pieceCache.nRows)
                                else pieceCache.nRows))

                for k in partitionKeyCols:
                    piecePandasDF[k] = pieceCache.partitionKVs[k]

            for mapper in self._mappers:
                piecePandasDF = mapper(piecePandasDF)

            results.append(piecePandasDF)

        return reducer(results)

    def __getattr__(self, col):
        assert col in self.columns

        return self.map(
                mapper=_ArrowADF__getattr__pandasDFTransform(col=col))

    def __getitem__(self, item):
        return self.map(
                mapper=_ArrowADF__getitem__pandasDFTransform(item=item))

    def drop(self, *cols, **kwargs):
        return self.map(
                mapper=_ArrowADF__drop__pandasDFTransform(cols=cols),
                **kwargs)

    def rename(self, **kwargs):
        """
        Return:
            ``ADF`` with new column names

        Args:
            **kwargs: arguments of the form ``newColName`` = ``existingColName``
        """
        renameDict = {}
        remainingKwargs = {}

        for k, v in kwargs.items():
            if v in self.columns:
                if v not in self._T_AUX_COLS:
                    renameDict[v] = k
            else:
                remainingKwargs[k] = v

        return self.map(
                mapper=lambda pandasDF:
                    pandasDF.rename(
                        mapper=None,
                        index=None,
                        columns=renameDict,
                        axis='index',
                        copy=False,
                        inplace=False,
                        level=None),
                **remainingKwargs)

    def filter(self, *conditions, **kwargs):
        pass   # TODO

    def collect(self, *cols, **kwargs):
        return self.reduce(cols=cols if cols else None, **kwargs)

    def toPandas(self, *cols, **kwargs):
        return self.collect(*cols, **kwargs)

    # *************************
    # KEY (SETTABLE) PROPERTIES
    # iCol
    # tCol

    @property
    def iCol(self):
        return self._iCol

    @iCol.setter
    def iCol(self, iCol):
        if iCol != self._iCol:
            self._iCol = iCol

            if iCol is None:
                self.hasTS = False
            else:
                assert iCol
                self.hasTS = bool(self._tCol)

    @iCol.deleter
    def iCol(self):
        self._iCol = None
        self.hasTS = False

    @property
    def tCol(self):
        return self._tCol

    @tCol.setter
    def tCol(self, tCol):
        if tCol != self._tCol:
            self._tCol = tCol

            if tCol is None:
                self.hasTS = False
            else:
                assert tCol
                self.hasTS = bool(self._iCol)

    @tCol.deleter
    def tCol(self):
        self._tCol = None
        self.hasTS = False

    # ***********
    # REPR SAMPLE
    # prelimReprSamplePiecePaths
    # reprSamplePiecePaths
    # _assignReprSample

    @property
    def prelimReprSamplePiecePaths(self):
        if self._cache.prelimReprSamplePiecePaths is None:
            self._cache.prelimReprSamplePiecePaths = \
                random.sample(
                    population=self.piecePaths,
                    k=self._reprSampleMinNPieces)

        return self._cache.prelimReprSamplePiecePaths

    @property
    def reprSamplePiecePaths(self):
        if self._cache.reprSamplePiecePaths is None:
            self._cache.reprSamplePiecePaths = \
                random.sample(
                    population=self.piecePaths,
                    k=self._reprSampleMinNPieces)

        return self._cache.reprSamplePiecePaths

    def _assignReprSample(self):
        self._cache.reprSample = \
            self.sample(
                n=self._reprSampleSize,
                piecePaths=self.reprSamplePiecePaths,
                verbose=True)

        self._reprSampleSize = len(self._cache.reprSample)

        self._cache.nonNullProportion = {}
        self._cache.suffNonNull = {}

    # *********************
    # ROWS, COLUMNS & TYPES
    # nRows
    # approxNRows
    # columns
    # types
    # type / typeIsNum / typeIsComplex

    @property
    def nRows(self):
        if self._cache.nRows is None:
            if arimo.debug.ON:
                self.stdout_logger.debug('*** COUNTING NO. OF ROWS ***')

            self._cache.nRows = \
                sum(read_metadata(where=self.pieceLocalOrHDFSPath(piecePath=piecePath)).num_rows
                    for piecePath in tqdm.tqdm(self.piecePaths))

        return self._cache.nRows

    @property
    def approxNRows(self):
        if self._cache.approxNRows is None:
            if arimo.debug.ON:
                self.stdout_logger.debug('*** COUNTING APPROX. NO. OF ROWS ***')

            self._cache.approxNRows = \
                self.nPieces \
                * sum(read_metadata(where=self.pieceLocalOrHDFSPath(piecePath=piecePath)).num_rows
                      for piecePath in tqdm.tqdm(self.prelimReprSamplePiecePaths)) \
                / self._reprSampleMinNPieces

        return self._cache.approxNRows

    @property
    def columns(self):
        return list(self.srcColsInclPartitionKVs) + \
            (list(self._T_AUX_COLS
                  if self._iCol
                  else self._T_COMPONENT_AUX_COLS)
             if self._tCol
             else [])

    @property
    def types(self):
        if self._tCol:
            _types = Namespace(
                **{col: self._T_AUX_COL_ARROW_TYPES[col]
                   for col in (self._T_AUX_COLS
                               if self._iCol
                               else self._T_COMPONENT_AUX_COLS)})

            for col, arrowType in self.srcTypesInclPartitionKVs.items():
                _types[col] = arrowType

            return _types

        else:
            return self.srcTypesInclPartitionKVs

    def type(self, col):
        return self.types[col]

    def typeIsNum(self, col):
        return is_num(self.type(col))

    def typeIsComplex(self, col):
        return is_complex(self.type(col))

    # *************
    # COLUMN GROUPS
    # indexCols
    # tRelAuxCols
    # possibleFeatureContentCols
    # possibleCatContentCols

    @property
    def indexCols(self):
        return ((self._iCol,) if self._iCol else ()) \
             + ((self._dCol,) if self._dCol else ()) \
             + ((self._tCol,) if self._tCol else ())

    @property
    def tRelAuxCols(self):
        return (self._T_ORD_COL, self._T_DELTA_COL) \
            if self.hasTS \
          else ()

    @property
    def possibleFeatureContentCols(self):
        chk = lambda t: is_boolean(t) or is_string(t) or is_num(t)

        return tuple(
            col for col in self.contentCols
                if chk(self.type(col)))

    @property
    def possibleCatContentCols(self):
        return tuple(
            col for col in self.contentCols
                if is_possible_cat(self.type(col)))

    # **************
    # SUBSET METHODS
    # _subset
    # filterByPartitionKeys
    # sample
    # gen

    def _subset(self, *piecePaths, **kwargs):
        if piecePaths:
            assert self.piecePaths.issuperset(piecePaths)

            nPiecePaths = len(piecePaths)

            if nPiecePaths == self.nPieces:
                return self

            else:
                if self.fromS3:
                    aws_access_key_id = self._srcArrowDS.fs.fs.key
                    aws_secret_access_key = self._srcArrowDS.fs.fs.secret

                else:
                    aws_access_key_id = aws_secret_access_key = None

                return ArrowADF(
                    path=tuple(sorted(piecePaths))
                        if len(piecePaths) > 1
                        else piecePaths[0],

                    aws_access_key_id=aws_access_key_id, aws_secret_access_key=aws_secret_access_key,

                    iCol=self._iCol, tCol=self._tCol,
                    _mappers=self._mappers,

                    reprSampleMinNPieces=self._reprSampleMinNPieces,
                    reprSampleSize=self._reprSampleSize,
        
                    minNonNullProportion=self._minNonNullProportion,
                    outlierTailProportion=self._outlierTailProportion,
                    maxNCats=self._maxNCats,
                    minProportionByMaxNCats=self._minProportionByMaxNCats,

                    **kwargs)

        else:
            return self

    def filterByPartitionKeys(self, *filterCriteriaTuples, **kwargs):
        filterCriteria = {}

        _samplePiecePath = next(iter(self.piecePaths))

        for filterCriteriaTuple in filterCriteriaTuples:
            assert isinstance(filterCriteriaTuple, (list, tuple))
            filterCriteriaTupleLen = len(filterCriteriaTuple)

            col = filterCriteriaTuple[0]

            if '{}='.format(col) in _samplePiecePath:
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
            piecePaths = set()

            for piecePath in self.piecePaths:
                chk = True

                for col, (fromVal, toVal, inSet) in filterCriteria.items():
                    v = re.search('{}=(.*?)/'.format(col), piecePath).group(1)

                    if ((fromVal is not None) and (v < fromVal)) or \
                            ((toVal is not None) and (v > toVal)) or \
                            ((inSet is not None) and (v not in inSet)):
                        chk = False
                        break

                if chk:
                    piecePaths.add(piecePath)

            assert piecePaths, \
                '*** {}: NO PIECE PATHS SATISFYING FILTER CRITERIA {} ***'.format(self, filterCriteria)

            if arimo.debug.ON:
                self.stdout_logger.debug(
                    msg='*** {} PIECES SATISFYING FILTERING CRITERIA: {} ***'
                        .format(len(piecePaths), filterCriteria))

            return self._subset(*piecePaths, **kwargs)

        else:
            return self

    def sample(self, *cols, **kwargs):
        n = kwargs.pop('n', self._DEFAULT_REPR_SAMPLE_SIZE)

        piecePaths = kwargs.pop('piecePaths', None)

        if piecePaths:
            nSamplePieces = len(piecePaths)

        else:
            minNPieces = kwargs.pop('minNPieces', self._reprSampleMinNPieces)
            maxNPieces = kwargs.pop('maxNPieces', None)

            nSamplePieces = \
                max(int(math.ceil(((min(n, self.approxNRows) / self.approxNRows) ** .5)
                                  * self.nPieces)),
                    minNPieces)

            if maxNPieces:
                nSamplePieces = min(nSamplePieces, maxNPieces)

            if nSamplePieces < self.nPieces:
                piecePaths = \
                    random.sample(
                        population=self.piecePaths,
                        k=nSamplePieces)

            else:
                nSamplePieces = self.nPieces
                piecePaths = self.piecePaths

        if arimo.debug.ON:
            self.stdout_logger.debug(
                '*** SAMPLING {:,} ROWS OF COLUMNS {} FROM {:,} PIECES ***'
                .format(n, cols, nSamplePieces))

        return self.reduce(
                *piecePaths,
                cols=cols,
                nSamplesPerPiece=int(math.ceil(n / nSamplePieces)),
                **kwargs)

    # ****************
    # COLUMN PROFILING
    # count
    # nonNullProportion
    # distinct
    # quantile
    # sampleStat
    # outlierRstStat / outlierRstMin / outlierRstMax
    # profile

    @_docstr_verbose
    def count(self, *cols, **kwargs):
        """
        Return:
            - If 1 column name is given, return its corresponding non-``NULL`` count

            - If multiple column names are given, return a {``col``: corresponding non-``NULL`` count} *dict*

            - If no column names are given, return a {``col``: corresponding non-``NULL`` count} *dict* for all columns

        Args:
             *cols (str): column name(s)

             **kwargs:
        """
        if not cols:
            cols = self.contentCols

        if len(cols) > 1:
            return Namespace(**
                {col: self.count(col, **kwargs)
                 for col in cols})

        else:
            col = cols[0]

            pandasDF = kwargs.get('pandasDF')

            if pandasDF is None:
                if col not in self._cache.count:
                    verbose = True \
                        if arimo.debug.ON \
                        else kwargs.get('verbose')

                    if verbose:
                        tic = time.time()

                    self._cache.count[col] = result = \
                        self[col] \
                        .map(mapper=lambda series: len(series.loc[pandas.notnull(series)])) \
                        .reduce(
                            cols=col,
                            reducer=sum)

                    assert isinstance(result, int), \
                        '*** "{}" COUNT = {} ***'.format(col, result)

                    if verbose:
                        toc = time.time()
                        self.stdout_logger.info(
                            msg='No. of Non-NULLs of Column "{}" = {:,}   <{:,.1f} s>'
                                .format(col, result, toc - tic))

                return self._cache.count[col]

            else:
                return len(pandasDF.loc[pandas.notnull(pandasDF[col])])

    @_docstr_verbose
    def nonNullProportion(self, *cols, **kwargs):
        """
        Return:
            - If 1 column name is given, return its *approximate* non-``NULL`` proportion

            - If multiple column names are given, return {``col``: approximate non-``NULL`` proportion} *dict*

            - If no column names are given, return {``col``: approximate non-``NULL`` proportion} *dict* for all columns

        Args:
             *cols (str): column name(s)

             **kwargs:
        """
        if not cols:
            cols = self.contentCols

        if len(cols) > 1:
            return Namespace(**
                {col: self.nonNullProportion(col, **kwargs)
                 for col in cols})

        else:
            col = cols[0]

            if col not in self._cache.nonNullProportion:
                self._cache.nonNullProportion[col] = \
                    self.count(
                        col,
                        pandasDF=self.reprSample,
                        **kwargs) \
                    / self.reprSampleSize

            return self._cache.nonNullProportion[col]

    @_docstr_verbose
    def distinct(self, *cols, **kwargs):
        """
        Return:
            *Approximate* list of distinct values of ``ADF``'s column ``col``,
                with optional descending-sorted counts for those values

        Args:
            col (str): name of a column

            count (bool): whether to count the number of appearances of each distinct value of the specified ``col``

            **kwargs:
        """
        if not cols:
            cols = self.contentCols

        if len(cols) > 1:
            return Namespace(**
                {col: self.distinct(col, **kwargs)
                 for col in cols})

        else:
            col = cols[0]

            count = kwargs.get('count', True)

            if col in self._cache.distinct:
                series = self._cache.distinct[col]

                assert isinstance(series, pandas.Series)

                if (series.dtype in PY_NUM_TYPES) or (not count):
                    return series

            self._cache.distinct[col] = \
                self.reprSample[col].value_counts(
                    normalize=True,
                    sort=True,
                    ascending=False,
                    bins=None,
                    dropna=False) \
                if count \
                else self.reprSample[col].unique()

            return self._cache.distinct[col]

    @lru_cache()
    def quantile(self, *cols, **kwargs):
        if len(cols) > 1:
            return Namespace(**
                {col: self.approxQuantile(col, **kwargs)
                 for col in cols}) \

        else:
            col = cols[0]

            return self[col] \
                .reduce(cols=col) \
                .quantile(
                    q=kwargs.get('q', .5),
                    interpolation='linear')

    @_docstr_verbose
    def sampleStat(self, *cols, **kwargs):
        """
        *Approximate* measurements of a certain statistic on **numerical** columns

        Args:
            *cols (str): column name(s)

            **kwargs:

                - **stat**: one of the following:

                    - ``avg``/``mean`` (default)
                    - ``median``
                    - ``min``
                    - ``max``
        """
        if not cols:
            cols = self.possibleNumContentCols

        if len(cols) > 1:
            return Namespace(**
                {col: self.sampleStat(col, **kwargs)
                 for col in cols})

        else:
            col = cols[0]

            if self.typeIsNum(col):
                stat = kwargs.pop('stat', 'mean').lower()
                if stat == 'avg':
                    stat = 'mean'
                capitalizedStatName = stat.capitalize()
                s = 'sample{}'.format(capitalizedStatName)

                if hasattr(self, s):
                    return getattr(self, s)(col, **kwargs)

                else:
                    if s not in self._cache:
                        setattr(self._cache, s, {})
                    cache = getattr(self._cache, s)

                    if col not in cache:
                        verbose = True \
                            if arimo.debug.ON \
                            else kwargs.get('verbose')

                        if verbose:
                            tic = time.time()

                        result = \
                            getattr(self.reprSample[col], stat)(
                                axis='index',
                                skipna=True,
                                level=None)

                        if isinstance(result, NUMPY_FLOAT_TYPES):
                            result = float(result)

                        elif isinstance(result, NUMPY_INT_TYPES):
                            result = int(result)

                        assert isinstance(result, PY_NUM_TYPES), \
                            '*** "{}" SAMPLE {} = {} ({}) ***'.format(
                                col, capitalizedStatName.upper(), result, type(result))

                        if verbose:
                            toc = time.time()
                            self.stdout_logger.info(
                                msg='Sample {} for Column "{}" = {:,.3g}   <{:,.1f} s>'
                                    .format(capitalizedStatName, col, result, toc - tic))

                        cache[col] = result

                    return cache[col]

            else:
                raise ValueError(
                    '{0}.sampleStat({1}, ...): Column "{1}" Is Not of Numeric Type'
                        .format(self, col))

    def outlierRstStat(self, *cols, **kwargs):
        if not cols:
            cols = self.possibleNumContentCols

        if len(cols) > 1:
            return Namespace(**
                {col: self.outlierRstStat(col, **kwargs)
                 for col in cols})

        else:
            col = cols[0]

            if self.typeIsNum(col):
                stat = kwargs.pop('stat', 'mean').lower()
                if stat == 'avg':
                    stat = 'mean'
                capitalizedStatName = stat.capitalize()
                s = 'outlierRst{}'.format(capitalizedStatName)

                if hasattr(self, s):
                    return getattr(self, s)(col, **kwargs)

                else:
                    if s not in self._cache:
                        setattr(self._cache, s, {})
                    cache = getattr(self._cache, s)

                    if col not in cache:
                        verbose = True \
                            if arimo.debug.ON \
                            else kwargs.get('verbose')

                        if verbose:
                            tic = time.time()

                        series = self.reprSample[col]

                        outlierTails = kwargs.pop('outlierTails', 'both')

                        if outlierTails == 'both':
                            series = series.loc[
                                series.between(
                                    left=self.outlierRstMin(col),
                                    right=self.outlierRstMax(col),
                                    inclusive=True)]

                        elif outlierTails == 'lower':
                            series = series.loc[series >= self.outlierRstMin(col)]

                        elif outlierTails == 'upper':
                            series = series.loc[series <= self.outlierRstMax(col)]

                        result = \
                            getattr(series, stat)(
                                axis='index',
                                skipna=True,
                                level=None)

                        if pandas.isnull(result):
                            self.stdout_logger.warning(
                                msg='*** "{}" OUTLIER-RESISTANT {} = {} ***'.format(col, capitalizedStatName.upper(), result))

                            result = self.outlierRstMin(col)

                        if isinstance(result, NUMPY_FLOAT_TYPES):
                            result = float(result)

                        elif isinstance(result, NUMPY_INT_TYPES):
                            result = int(result)

                        assert isinstance(result, PY_NUM_TYPES), \
                            '*** "{}" OUTLIER-RESISTANT {} = {} ({}) ***'.format(
                                col, capitalizedStatName.upper(), result, type(result))

                        if verbose:
                            toc = time.time()
                            self.stdout_logger.info(
                                msg='Outlier-Resistant {} for Column "{}" = {:,.3g}   <{:,.1f} s>'
                                    .format(capitalizedStatName, col, result, toc - tic))

                        cache[col] = result

                    return cache[col]

            else:
                raise ValueError(
                    '{0}.outlierRstStat({1}, ...): Column "{1}" Is Not of Numeric Type'
                        .format(self, col))

    def outlierRstMin(self, *cols, **kwargs):
        if not cols:
            cols = self.possibleNumContentCols

        if len(cols) > 1:
            return Namespace(**
                {col: self.outlierRstMin(col, **kwargs)
                 for col in cols})

        else:
            col = cols[0]

            if self.typeIsNum(col):
                if 'outlierRstMin' not in self._cache:
                    self._cache.outlierRstMin = {}

                if col not in self._cache.outlierRstMin:
                    verbose = True \
                        if arimo.debug.ON \
                        else kwargs.get('verbose')

                    if verbose:
                        tic = time.time()

                    series = self.reprSample[col]

                    outlierRstMin = \
                        series.quantile(
                            q=self._outlierTailProportion[col],
                            interpolation='linear')

                    sampleMin = self.sampleStat(col, stat='min')
                    sampleMedian = self.sampleStat(col, stat='median')

                    result = \
                        series.loc[series > sampleMin] \
                            .min(axis='index',
                                 skipna=True,
                                 level=None) \
                        if (outlierRstMin == sampleMin) and (outlierRstMin < sampleMedian) \
                        else outlierRstMin

                    if isinstance(result, NUMPY_FLOAT_TYPES):
                        result = float(result)

                    elif isinstance(result, NUMPY_INT_TYPES):
                        result = int(result)

                    assert isinstance(result, PY_NUM_TYPES), \
                        '*** "{}" OUTLIER-RESISTANT MIN = {} ({}) ***'.format(col, result, type(result))

                    if verbose:
                        toc = time.time()
                        self.stdout_logger.info(
                            msg='Outlier-Resistant Min of Column "{}" = {:,.3g}   <{:,.1f} s>'
                                .format(col, result, toc - tic))

                    self._cache.outlierRstMin[col] = result

                return self._cache.outlierRstMin[col]

            else:
                raise ValueError(
                    '{0}.outlierRstMin({1}, ...): Column "{1}" Is Not of Numeric Type'
                        .format(self, col))

    def outlierRstMax(self, *cols, **kwargs):
        if not cols:
            cols = self.possibleNumContentCols

        if len(cols) > 1:
            return Namespace(**
                {col: self.outlierRstMax(col, **kwargs)
                 for col in cols})

        else:
            col = cols[0]

            if self.typeIsNum(col):
                if 'outlierRstMax' not in self._cache:
                    self._cache.outlierRstMax = {}

                if col not in self._cache.outlierRstMax:
                    verbose = True \
                        if arimo.debug.ON \
                        else kwargs.get('verbose')

                    if verbose:
                        tic = time.time()

                    series = self.reprSample[col]

                    outlierRstMax = \
                        series.quantile(
                            q=1 - self._outlierTailProportion[col],
                            interpolation='linear')

                    sampleMax = self.sampleStat(col, stat='max')
                    sampleMedian = self.sampleStat(col, stat='median')

                    result = \
                        series.loc[series < sampleMax] \
                            .max(axis='index',
                                 skipna=True,
                                 level=None) \
                        if (outlierRstMax == sampleMax) and (outlierRstMax > sampleMedian) \
                        else outlierRstMax

                    if isinstance(result, NUMPY_FLOAT_TYPES):
                        result = float(result)

                    elif isinstance(result, NUMPY_INT_TYPES):
                        result = int(result)

                    assert isinstance(result, PY_NUM_TYPES), \
                        '*** "{}" OUTLIER-RESISTANT MAX = {} ({}) ***'.format(col, result, type(result))

                    if verbose:
                        toc = time.time()
                        self.stdout_logger.info(
                            msg='Outlier-Resistant Max of Column "{}" = {:,.3g}   <{:,.1f} s>'
                                .format(col, result, toc - tic))

                    self._cache.outlierRstMax[col] = result

                return self._cache.outlierRstMax[col]

            else:
                raise ValueError(
                    '{0}.outlierRstMax({1}, ...): Column "{1}" Is Not of Numeric Type'
                        .format(self, col))

    @_docstr_verbose
    def profile(self, *cols, **kwargs):
        """
        Return:
            *dict* of profile of salient statistics on specified columns of ``ADF``

        Args:
            *cols (str): names of column(s) to profile

            **kwargs:

                - **profileCat** *(bool, default = True)*: whether to profile possible categorical columns

                - **profileNum** *(bool, default = True)*: whether to profile numerical columns

                - **skipIfInsuffNonNull** *(bool, default = False)*: whether to skip profiling if column does not have enough non-NULLs
        """
        if not cols:
            cols = self.contentCols

        asDict = kwargs.pop('asDict', False)

        if len(cols) > 1:
            return Namespace(**
                {col: self.profile(col, **kwargs)
                 for col in cols})

        else:
            col = cols[0]

            verbose = True \
                if arimo.debug.ON \
                else kwargs.get('verbose')

            if verbose:
                msg = 'Profiling Column "{}"...'.format(col)
                self.stdout_logger.info(msg)
                tic = time.time()

            colType = self.type(col)
            profile = Namespace(type=colType)

            # non-NULL Proportions
            profile.nonNullProportion = \
                self.nonNullProportion(
                    col,
                    verbose=verbose > 1)

            if self.suffNonNull(col) or (not kwargs.get('skipIfInsuffNonNull', False)):
                # profile categorical column
                if kwargs.get('profileCat', True) and is_possible_cat(colType):
                    profile.distinctProportions = \
                        self.distinct(
                            col,
                            count=True,
                            verbose=verbose > 1)

                # profile numerical column
                if kwargs.get('profileNum', True) and is_num(colType):
                    outlierTailProportion = self._outlierTailProportion[col]

                    quantilesOfInterest = \
                        pandas.Series(
                            index=(0,
                                   outlierTailProportion,
                                   .5,
                                   1 - outlierTailProportion,
                                   1))
                    quantileProbsToQuery = []

                    sampleMin = self._cache.sampleMin.get(col)
                    if sampleMin:
                        quantilesOfInterest[0] = sampleMin
                        toCacheSampleMin = False
                    else:
                        quantileProbsToQuery += [0.]
                        toCacheSampleMin = True

                    outlierRstMin = self._cache.outlierRstMin.get(col)
                    if outlierRstMin:
                        quantilesOfInterest[outlierTailProportion] = outlierRstMin
                        toCacheOutlierRstMin = False
                    else:
                        quantileProbsToQuery += [outlierTailProportion]
                        toCacheOutlierRstMin = True

                    sampleMedian = self._cache.sampleMedian.get(col)
                    if sampleMedian:
                        quantilesOfInterest[.5] = sampleMedian
                        toCacheSampleMedian = False
                    else:
                        quantileProbsToQuery += [.5]
                        toCacheSampleMedian = True

                    outlierRstMax = self._cache.outlierRstMax.get(col)
                    if outlierRstMax:
                        quantilesOfInterest[1 - outlierTailProportion] = outlierRstMax
                        toCacheOutlierRstMax = False
                    else:
                        quantileProbsToQuery += [1 - outlierTailProportion]
                        toCacheOutlierRstMax = True

                    sampleMax = self._cache.sampleMax.get(col)
                    if sampleMax:
                        quantilesOfInterest[1] = sampleMax
                        toCacheSampleMax = False
                    else:
                        quantileProbsToQuery += [1.]
                        toCacheSampleMax = True

                    series = self.reprSample[col]

                    if quantileProbsToQuery:
                        quantilesOfInterest[numpy.isnan(quantilesOfInterest)] = \
                            series.quantile(
                                q=quantileProbsToQuery,
                                interpolation='linear')

                    sampleMin, outlierRstMin, sampleMedian, outlierRstMax, sampleMax = quantilesOfInterest

                    if toCacheSampleMin:
                        self._cache.sampleMin[col] = sampleMin

                    if toCacheOutlierRstMin:
                        if (outlierRstMin == sampleMin) and (outlierRstMin < sampleMedian):
                            outlierRstMin = \
                                series.loc[series > sampleMin] \
                                    .min(axis='index',
                                         skipna=True,
                                         level=None)
                        self._cache.outlierRstMin[col] = outlierRstMin

                    if toCacheSampleMedian:
                        self._cache.sampleMedian[col] = sampleMedian

                    if toCacheOutlierRstMax:
                        if (outlierRstMax == sampleMax) and (outlierRstMax > sampleMedian):
                            outlierRstMax = \
                                series.loc[series < sampleMax] \
                                    .max(axis='index',
                                         skipna=True,
                                         level=None)
                        self._cache.outlierRstMax[col] = outlierRstMax

                    if toCacheSampleMax:
                        self._cache.sampleMax[col] = sampleMax

                    profile.sampleRange = sampleMin, sampleMax
                    profile.outlierRstRange = outlierRstMin, outlierRstMax

                    profile.sampleMean = \
                        self.sampleStat(
                            col,
                            stat='mean',
                            verbose=verbose)

                    profile.outlierRstMean = \
                        self._cache.outlierRstMean.get(
                            col,
                            self.outlierRstStat(
                                col,
                                stat='mean',
                                verbose=verbose))

                    profile.outlierRstMedian = \
                        self._cache.outlierRstMedian.get(
                            col,
                            self.outlierRstStat(
                                col,
                                stat='median',
                                verbose=verbose))

            if verbose:
                toc = time.time()
                self.stdout_logger.info(msg + ' done!   <{:,.1f} s>'.format(toc - tic))

            return Namespace(**{col: profile}) \
                if asDict \
                else profile

    # *********
    # DATA PREP
    # fillna
    # prep

    @_docstr_verbose
    def fillna(self, *cols, **kwargs):
        """
        Fill/interpolate ``NULL``/``NaN`` values

        Return:
            ``ADF`` with ``NULL``/``NaN`` values filled/interpolated

        Args:
            *args (str): names of column(s) to fill/interpolate

            **kwargs:

                - **method** *(str)*: one of the following methods to fill ``NULL`` values in **numerical** columns,
                    or *dict* of such method specifications by column name

                    - ``avg``/``mean`` (default)
                    - ``min``
                    - ``max``
                    - ``avg_before``/``mean_before``
                    - ``min_before``
                    - ``max_before``
                    - ``avg_after``/``mean_after``
                    - ``min_after``
                    - ``max_after``
                    - ``linear`` (**TO-DO**)
                    - ``before`` (**TO-DO**)
                    - ``after`` (**TO-DO**)
                    - ``None`` (do nothing)

                    (*NOTE:* for an ``ADF`` with a ``.tCol`` set, ``NumPy/Pandas NaN`` values cannot be filled;
                        it is best that such *Python* values be cleaned up before they get into Spark)

                - **value**: single value, or *dict* of values by column name,
                    to use if ``method`` is ``None`` or not applicable

                - **outlierTails** *(str or dict of str, default = 'both')*: specification of in which distribution tail (``None``, ``lower``, ``upper`` and ``both`` (default)) of each numerical column out-lying values may exist

                - **fillOutliers** *(bool or list of column names, default = False)*: whether to treat detected out-lying values as ``NULL`` values to be replaced in the same way

                - **loadPath** *(str)*: path to load existing ``NULL``-filling data transformations

                - **savePath** *(str)*: path to save new ``NULL``-filling data transformations
        """
        _TS_FILL_METHODS = \
            'avg_partition', 'mean_partition', 'min_partition', 'max_partition', \
            'avg_before', 'mean_before', 'min_before', 'max_before', \
            'avg_after', 'mean_after', 'min_after', 'max_after'

        if self.hasTS:
            _TS_OPPOSITE_METHODS = \
                Namespace(
                    avg='avg',
                    mean='mean',
                    min='max',
                    max='min')

            _TS_WINDOW_NAMES = \
                Namespace(
                    partition='partitionByI',
                    before='partitionByI_orderByT_before',
                    after='partitionByI_orderByT_after')

            _TS_OPPOSITE_WINDOW_NAMES = \
                Namespace(
                    partition='partition',
                    before='after',
                    after='before')

            _TS_WINDOW_DEFS = \
                Namespace(
                    partition=
                        '{} AS (PARTITION BY {}, {})'
                            .format(_TS_WINDOW_NAMES.partition, self._iCol, SparkADF._T_CHUNK_COL),
                    before=
                        '{} AS (PARTITION BY {}, {} ORDER BY {} ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)'
                            .format(_TS_WINDOW_NAMES.before, self._iCol, SparkADF._T_CHUNK_COL, self._T_ORD_COL),
                    after=
                        '{} AS (PARTITION BY {}, {} ORDER BY {} ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING)'
                            .format(_TS_WINDOW_NAMES.after, self._iCol, SparkADF._T_CHUNK_COL, self._T_ORD_COL))

        returnDetails = kwargs.pop('returnDetails', False)
        returnSQLStatement = kwargs.pop('returnSQLStatement', False)
        loadPath = kwargs.pop('loadPath', None)
        savePath = kwargs.pop('savePath', None)

        verbose = kwargs.pop('verbose', False)
        if arimo.debug.ON:
            verbose = True

        _medianFill = kwargs.pop('_medianFill', False)

        if loadPath:
            if verbose:
                message = 'Loading NULL-Filling SQL Statement from Path "{}"...'.format(loadPath)
                self.stdout_logger.info(message)
                tic = time.time()

            sqlStatement = \
                json.load(
                    open(os.path.join(loadPath, self._NULL_FILL_SQL_STATEMENT_FILE_NAME), 'r'))

            details = None

        else:
            value = kwargs.pop('value', None)

            method = kwargs.pop(
                'method',
                'mean' if value is None
                       else None)

            cols = set(cols)

            if isinstance(method, dict):
                cols.update(method)

            if isinstance(value, dict):
                cols.update(value)

            if not cols:
                cols = set(self.contentCols)

            cols.difference_update(
                self.indexCols +
                (self._T_ORD_COL,))

            nulls = kwargs.pop('nulls', {})

            for col in cols:
                if col in nulls:
                    colNulls = nulls[col]

                    assert isinstance(colNulls, (list, tuple)) and (len(colNulls) == 2) \
                       and ((colNulls[0] is None) or isinstance(colNulls[0], PY_NUM_TYPES)) \
                       and ((colNulls[1] is None) or isinstance(colNulls[1], PY_NUM_TYPES))

                else:
                    nulls[col] = (None, None)

            outlierTails = kwargs.pop('outlierTails', {})
            if isinstance(outlierTails, _STR_CLASSES):
                outlierTails = \
                    {col: outlierTails
                     for col in cols}

            fillOutliers = kwargs.pop('fillOutliers', False)
            fillOutliers = \
                cols \
                if fillOutliers is True \
                else to_iterable(fillOutliers)

            tsWindowDefs = set()
            details = {}

            if verbose:
                message = 'NULL-Filling Columns {}...'.format(
                    ', '.join('"{}"'.format(col) for col in cols))
                self.stdout_logger.info(message)
                tic = time.time()

            for col in cols:
                colType = self.type(col)
                colFallBackVal = None

                if is_num(colType):
                    isNum = True

                    colOutlierTails = outlierTails.get(col, 'both')
                    fixLowerTail = colOutlierTails in ('lower', 'both')
                    fixUpperTail = colOutlierTails in ('upper', 'both')

                    methodForCol = \
                        method[col] \
                        if isinstance(method, dict) and (col in method) \
                        else method

                    if methodForCol:
                        methodForCol = methodForCol.lower().split('_')

                        if len(methodForCol) == 2:
                            assert self.hasTS, \
                                "NULL-Filling Methods {} Not Supported for Non-Time-Series ADFs".format(
                                    ', '.join(s.upper() for s in _TS_FILL_METHODS))

                            methodForCol, window = methodForCol

                        else:
                            methodForCol = methodForCol[0]

                            if self.hasTS:
                                window = None

                        colFallBackVal = \
                            self.outlierRstStat(
                                col,
                                stat=methodForCol
                                    if (not self.hasTS) or (window is None) or (window == 'partition')
                                    else 'mean',
                                outlierTails=colOutlierTails,
                                verbose=verbose > 1)

                    elif isinstance(value, dict):
                        colFallBackVal = value.get(col)
                        if not isinstance(colFallBackVal, _NUM_CLASSES):
                            colFallBackVal = None

                    elif isinstance(value, _NUM_CLASSES):
                        colFallBackVal = value

                else:
                    isNum = False

                    if isinstance(value, dict):
                        colFallBackVal = value.get(col)
                        if isinstance(colFallBackVal, _NUM_CLASSES):
                            colFallBackVal = None

                    elif not isinstance(value, _NUM_CLASSES):
                        colFallBackVal = value

                if pandas.notnull(colFallBackVal):
                    fallbackStrs = \
                        ["'{}'".format(colFallBackVal)
                         if is_string(colType) and isinstance(colFallBackVal, _STR_CLASSES)
                         else repr(colFallBackVal)]

                    lowerNull, upperNull = colNulls = nulls[col]

                    if isNum and self.hasTS and window:
                        partitionFallBackStrTemplate = \
                            "{}(CASE WHEN (STRING({}) = 'NaN'){}{}{}{} THEN NULL ELSE {} END) OVER {}"

                        fallbackStrs.insert(
                            0,
                            partitionFallBackStrTemplate.format(
                                methodForCol,
                                col,
                                '' if lowerNull is None
                                   else ' OR ({} <= {})'.format(col, lowerNull),
                                '' if upperNull is None
                                   else ' OR ({} >= {})'.format(col, upperNull),
                                ' OR ({} < {})'.format(col, self.outlierRstMin(col))
                                    if fixLowerTail
                                    else '',
                                ' OR ({} > {})'.format(col, self.outlierRstMax(col))
                                    if fixUpperTail
                                    else '',
                                col,
                                _TS_WINDOW_NAMES[window]))
                        tsWindowDefs.add(_TS_WINDOW_DEFS[window])

                        if window != 'partition':
                            oppositeWindow = _TS_OPPOSITE_WINDOW_NAMES[window]
                            fallbackStrs.insert(
                                1,
                                partitionFallBackStrTemplate.format(
                                    _TS_OPPOSITE_METHODS[methodForCol],
                                    col,
                                    '' if lowerNull is None
                                       else ' OR ({} <= {})'.format(col, lowerNull),
                                    '' if upperNull is None
                                       else ' OR ({} >= {})'.format(col, upperNull),
                                    ' OR ({} < {})'.format(col, self.outlierRstMin(col))
                                        if fixLowerTail
                                        else '',
                                    ' OR ({} > {})'.format(col, self.outlierRstMax(col))
                                        if fixUpperTail
                                        else '',
                                    col,
                                    _TS_WINDOW_NAMES[oppositeWindow]))
                            tsWindowDefs.add(_TS_WINDOW_DEFS[oppositeWindow])

                    details[col] = \
                        [self._NULL_FILL_PREFIX + col + self._PREP_SUFFIX,

                         dict(SQL="COALESCE(CASE WHEN (STRING({0}) = 'NaN'){1}{2}{3}{4} THEN NULL ELSE {0} END, {5})"
                             .format(
                                col,
                                '' if lowerNull is None
                                   else ' OR ({} <= {})'.format(col, lowerNull),
                                '' if upperNull is None
                                   else ' OR ({} >= {})'.format(col, upperNull),
                                ' OR ({} < {})'.format(col, self.outlierRstMin(col))
                                    if isNum and (col in fillOutliers) and fixLowerTail
                                    else '',
                                ' OR ({} > {})'.format(col, self.outlierRstMax(col))
                                    if isNum and (col in fillOutliers) and fixUpperTail
                                    else '',
                                ', '.join(fallbackStrs)),

                             Nulls=colNulls,
                             NullFillValue=colFallBackVal)]

            if tsWindowDefs:
                details['__TS_WINDOW_CLAUSE__'] = \
                    _tsWindowClause = \
                    'WINDOW {}'.format(', '.join(tsWindowDefs))

            else:
                _tsWindowClause = ''

            sqlStatement = \
                'SELECT *, {} FROM __THIS__ {}'.format(
                    ', '.join(
                        '{} AS {}'.format(nullFillDetails['SQL'], nullFillCol)
                        for col, (nullFillCol, nullFillDetails) in details.items()
                        if col != '__TS_WINDOW_CLAUSE__'),
                    _tsWindowClause)

        if savePath and (savePath != loadPath):
            if verbose:
                msg = 'Saving NULL-Filling SQL Statement to Path "{}"...'.format(savePath)
                self.stdout_logger.info(msg)
                _tic = time.time()

            fs.mkdir(
                dir=savePath,
                hdfs=False)

            json.dump(
                sqlStatement,
                open(os.path.join(savePath, self._NULL_FILL_SQL_STATEMENT_FILE_NAME), 'w'),
                indent=2)

            if verbose:
                _toc = time.time()
                self.stdout_logger.info(msg + ' done!   <{:,.1f} s>'.format(_toc - _tic))

        arrowADF = \
            self.map(
                mapper=_ArrowADF__fillna__pandasDFTransform(
                        nullFillDetails=details,
                        _medianFill=_medianFill),
                inheritNRows=True,
                **kwargs)

        arrowADF._inheritCache(
            self,
            *(() if loadPath
                 else cols))

        arrowADF._cache.reprSample = self._cache.reprSample

        if verbose:
            toc = time.time()
            self.stdout_logger.info(message + ' done!   <{:,.1f} m>'.format((toc - tic) / 60))

        return ((arrowADF, details, sqlStatement)
                if returnSQLStatement
                else (arrowADF, details)) \
            if returnDetails \
          else arrowADF

    @_docstr_verbose
    def prep(self, *cols, **kwargs):
        """
        Pre-process ``ADF``'s selected column(s) in standard ways:
            - One-hot-encode categorical columns
            - Scale numerical columns

        Return:
            Standard-pre-processed ``ADF``

        Args:
            *args: column(s) to pre-process

            **kwargs:
                - **forceCat** *(str or list/tuple of str, default = None)*: columns to force to be categorical variables

                - **forceNum** *(str or list/tuple of str, default = None)*: columns to force to be numerical variables

                - **fill**:
                    - *dict* ( ``method`` = ... *(default: 'mean')*, ``value`` = ... *(default: None)*, ``outlierTails`` = ... *(default: False)*, ``fillOutliers`` = ... *(default: False)*) as per ``.fillna(...)`` method;
                    - *OR* ``None`` to not apply any ``NULL``/``NaN``-filling

                - **scaler** *(str)*: one of the following methods to use on numerical columns
                    (*ignored* if loading existing ``prep`` pipeline from ``loadPath``):

                    - ``standard`` (default)
                    - ``maxabs``
                    - ``minmax``
                    - ``None`` *(do not apply any scaling)*

                - **assembleVec** *(str, default = '__X__')*: name of vector column to build from pre-processed features; *ignored* if loading existing ``prep`` pipeline from ``loadPath``

                - **loadPath** *(str)*: path to load existing data transformations

                - **savePath** *(str)*: path to save new fitted data transformations
        """
        def sqlStdScl(sqlItem, mean, std):
            return '(({}) - {}) / {}'.format(sqlItem, mean, std)

        def sqlMaxAbsScl(sqlItem, maxAbs):
            return '({}) / {}'.format(sqlItem, maxAbs)

        def sqlMinMaxScl(sqlItem, origMin, origMax, targetMin, targetMax):
            origRange = origMax - origMin
            targetRange = targetMax - targetMin
            return '({} * (({}) - ({})) / {}) + ({})'.format(
                targetRange, sqlItem, origMin, origRange, targetMin)

        kwargs.pop('alias', None)   # *** NOT USED ***

        nulls = kwargs.pop('nulls', {})

        forceCatIncl = kwargs.pop('forceCatIncl', None)
        forceCatExcl = kwargs.pop('forceCatExcl', None)
        forceCat = kwargs.pop('forceCat', None)
        forceCat = \
            (set()
             if forceCat is None
             else to_iterable(forceCat, iterable_type=set)) \
            .union(
                ()
                if forceCatIncl is None
                else to_iterable(forceCatIncl)) \
            .difference(
                ()
                if forceCatExcl is None
                else to_iterable(forceCatExcl))

        kwargs.pop('oheCat', None)   # *** NOT USED ***
        scaleCat = kwargs.pop('scaleCat', True)

        forceNumIncl = kwargs.pop('forceNumIncl', None)
        forceNumExcl = kwargs.pop('forceNumExcl', None)
        forceNum = kwargs.pop('forceNum', None)
        forceNum = \
            (set()
             if forceNum is None
             else to_iterable(forceNum, iterable_type=set)) \
            .union(
                ()
                if forceNumIncl is None
                else to_iterable(forceNumIncl)) \
            .difference(
                ()
                if forceNumExcl is None
                else to_iterable(forceNumExcl))

        fill = kwargs.pop(
            'fill',
            dict(method='mean',
                 value=None,
                 outlierTails='both',
                 fillOutliers=False))

        assert fill, '*** {}.prep(...) MUST INVOLVE NULL-FILLING FOR NUMERIC COLS ***'.format(type(self))

        scaler = kwargs.pop('scaler', 'standard')
        if scaler:
            scaler = scaler.lower()

        kwargs.pop('assembleVec', None)   # *** NOT USED ***

        returnOrigToPrepColMaps = kwargs.pop('returnOrigToPrepColMaps', False)
        returnSQLStatement = kwargs.pop('returnSQLStatement', False)

        loadPath = kwargs.pop('loadPath', None)
        savePath = kwargs.pop('savePath', None)

        verbose = kwargs.pop('verbose', False)
        if arimo.debug.ON:
            verbose = True

        _medianFill = kwargs.pop('_medianFill', False)

        if loadPath:
            if verbose:
                message = 'Loading & Applying Data Transformations from Path "{}"...'.format(loadPath)
                self.stdout_logger.info(message)
                tic = time.time()

            if loadPath in self._PREP_CACHE:
                prepCache = self._PREP_CACHE[loadPath]

                catOrigToPrepColMap = prepCache.catOrigToPrepColMap
                numOrigToPrepColMap = prepCache.numOrigToPrepColMap
                defaultVecCols = prepCache.defaultVecCols

                sqlStatement = prepCache.sqlStatement
                sqlTransformer = prepCache.sqlTransformer

                catOHETransformer = prepCache.catOHETransformer
                pipelineModelWithoutVectors = prepCache.pipelineModelWithoutVectors

            else:
                if fs._ON_LINUX_CLUSTER_WITH_HDFS:
                    localDirExists = os.path.isdir(loadPath)

                    hdfsDirExists = \
                        arimo.backend.hdfs.test(
                            path=loadPath,
                            exists=True,
                            directory=True)

                    if localDirExists and (not hdfsDirExists):
                        fs.put(
                            from_local=loadPath,
                            to_hdfs=loadPath,
                            is_dir=True,
                            _mv=False)

                    elif hdfsDirExists and (not localDirExists):
                        fs.get(
                            from_hdfs=loadPath,
                            to_local=loadPath,
                            is_dir=True,
                            overwrite=True, _mv=False,
                            must_succeed=True,
                            _on_linux_cluster_with_hdfs=True)

                catOrigToPrepColMap = \
                    json.load(open(os.path.join(loadPath, self._CAT_ORIG_TO_PREP_COL_MAP_FILE_NAME), 'r'))

                numOrigToPrepColMap = \
                    json.load(open(os.path.join(loadPath, self._NUM_ORIG_TO_PREP_COL_MAP_FILE_NAME), 'r'))

                defaultVecCols = \
                    [catOrigToPrepColMap[catCol][0]
                     for catCol in sorted(set(catOrigToPrepColMap)
                                          .difference(('__OHE__', '__SCALE__')))] + \
                    [numOrigToPrepColMap[numCol][0]
                     for numCol in sorted(set(numOrigToPrepColMap)
                                          .difference(('__TS_WINDOW_CLAUSE__', '__SCALER__')))]

                sqlStatement = \
                    json.load(open(os.path.join(loadPath, self._PREP_SQL_STATEMENT_FILE_NAME), 'r'))

                self._PREP_CACHE[loadPath] = \
                    Namespace(
                        catOrigToPrepColMap=catOrigToPrepColMap,
                        numOrigToPrepColMap=numOrigToPrepColMap,
                        defaultVecCols=defaultVecCols,

                        sqlStatement=sqlStatement,
                        sqlTransformer=None,

                        catOHETransformer=None,
                        pipelineModelWithoutVectors=None)

        else:
            if cols:
                cols = set(cols)

                cols = cols.intersection(self.possibleFeatureTAuxCols).union(
                        possibleFeatureContentCol for possibleFeatureContentCol in cols.intersection(self.possibleFeatureContentCols)
                                                  if self.suffNonNull(possibleFeatureContentCol))

            else:
                cols = self.possibleFeatureTAuxCols + \
                        tuple(possibleFeatureContentCol for possibleFeatureContentCol in self.possibleFeatureContentCols
                                                        if self.suffNonNull(possibleFeatureContentCol))

            if cols:
                profile = \
                    self.profile(
                        *cols,
                        profileCat=True,
                        profileNum=False,   # or bool(fill) or bool(scaler)?
                        skipIfInsuffNonNull=True,
                        asDict=True,
                        verbose=verbose)

            else:
                return self.copy()

            cols = {col for col in cols
                    if self.suffNonNull(col) and
                        (len(profile[col].distinctProportions
                            .loc[# (profile[col].distinctProportions.index != '') &
                                 # FutureWarning: elementwise comparison failed; returning scalar instead,
                                 # but in the future will perform elementwise comparison
                                 pandas.notnull(profile[col].distinctProportions.index)]) > 1)}

            if not cols:
                return self.copy()

            catCols = \
                [col for col in cols.intersection(self.possibleCatCols).difference(forceNum)
                     if (col in forceCat) or
                        (profile[col].distinctProportions.iloc[:self._maxNCats[col]].sum()
                         >= self._minProportionByMaxNCats[col])]

            numCols = [col for col in cols.difference(catCols)
                           if self.typeIsNum(col)]

            cols = catCols + numCols

            if verbose:
                message = 'Prepping Columns {}...'.format(', '.join('"{}"'.format(col) for col in cols))
                self.stdout_logger.info(message)
                tic = time.time()

            prepSqlItems = {}

            catOrigToPrepColMap = \
                dict(__OHE__=False,
                     __SCALE__=scaleCat)

            if catCols:
                if verbose:
                    msg = 'Transforming Categorical Features {}...'.format(
                        ', '.join('"{}"'.format(catCol) for catCol in catCols))
                    self.stdout_logger.info(msg)
                    _tic = time.time()

                catIdxCols = []

                if scaleCat:
                    catScaledIdxCols = []

                for catCol in catCols:
                    catIdxCol = self._CAT_IDX_PREFIX + catCol + self._PREP_SUFFIX

                    catColType = self.type(catCol)

                    if is_boolean(catColType):
                        cats = [0, 1]

                        nCats = 2

                        catIdxSqlItem = \
                            'CASE WHEN {0} IS NULL THEN 2 \
                                  WHEN {0} THEN 1 \
                                  ELSE 0 END'.format(catCol)

                    else:
                        isStr = is_string(catColType)

                        cats = [cat for cat in
                                    (profile[catCol].distinctProportions.index
                                     if catCol in forceCat
                                     else profile[catCol].distinctProportions.index[:self._maxNCats[catCol]])
                                if (cat != '') and pandas.notnull(cat)]

                        nCats = len(cats)

                        catIdxSqlItem = \
                            'CASE {} ELSE {} END'.format(
                                ' '.join('WHEN {} = {} THEN {}'.format(
                                            catCol,
                                            "'{}'".format(cat.replace("'", "''").replace('"', '""'))
                                                if isStr
                                                else cat,
                                            i)
                                         for i, cat in enumerate(cats)),
                                nCats)

                    if scaleCat:
                        catPrepCol = self._MIN_MAX_SCL_PREFIX + self._CAT_IDX_PREFIX + catCol + self._PREP_SUFFIX
                        catScaledIdxCols.append(catPrepCol)

                        prepSqlItems[catPrepCol] = \
                            sqlMinMaxScl(
                                sqlItem=catIdxSqlItem,
                                origMin=0, origMax=nCats,
                                targetMin=-1, targetMax=1)

                    else:
                        prepSqlItems[catIdxCol] = catIdxSqlItem

                        catPrepCol = catIdxCol

                    catOrigToPrepColMap[catCol] = \
                        [catPrepCol,

                         dict(Cats=cats,
                              NCats=nCats)]

                if verbose:
                    _toc = time.time()
                    self.stdout_logger.info(msg + ' done!   <{:,.1f} s>'.format(_toc - tic))

            numOrigToPrepColMap = \
                dict(__SCALER__=scaler)

            if numCols:
                numScaledCols = []

                if verbose:
                    msg = 'Transforming Numerical Features {}...'.format(
                        ', '.join('"{}"'.format(numCol) for numCol in numCols))
                    self.stdout_logger.info(msg)
                    _tic = time.time()

                outlierTails = fill.get('outlierTails', {})
                if isinstance(outlierTails, _STR_CLASSES):
                    outlierTails = \
                        {col: outlierTails
                         for col in numCols}

                _, numNullFillDetails = \
                    self.fillna(
                        *numCols,
                        nulls=nulls,
                        method=fill.get('method', 'mean'),
                        value=fill.get('value'),
                        outlierTails=outlierTails,
                        fillOutliers=fill.get('fillOutliers', False),
                        returnDetails=True,
                        verbose=verbose > 1,
                        _medianFill=_medianFill)

                for numCol in numCols:
                    colOutlierTails = outlierTails.get(numCol, 'both')

                    excludeLowerTail = colOutlierTails in ('lower', 'both')
                    colMin = self.outlierRstMin(numCol) \
                        if excludeLowerTail \
                        else self.sampleStat(numCol, stat='min')

                    excludeUpperTail = colOutlierTails in ('upper', 'both')
                    colMax = self.outlierRstMax(numCol) \
                        if excludeUpperTail \
                        else self.sampleStat(numCol, stat='max')

                    if colMin < colMax:
                        numColNullFillDetails = numNullFillDetails[numCol][1]

                        numColSqlItem = numColNullFillDetails['SQL']
                        numColNulls = numColNullFillDetails['Nulls']

                        numColNullFillValue = numColNullFillDetails['NullFillValue']
                        assert numpy.allclose(numColNullFillValue, self.outlierRstStat(numCol))

                        if scaler:
                            if scaler == 'standard':
                                scaledCol = self._STD_SCL_PREFIX + numCol + self._PREP_SUFFIX

                                series = self.reprSample[numCol]

                                if colOutlierTails == 'both':
                                    series = series.loc[
                                        series.between(
                                            left=colMin,
                                            right=colMax,
                                            inclusive=True)]

                                elif colOutlierTails == 'lower':
                                    series = series.loc[series > colMin]

                                elif colOutlierTails == 'upper':
                                    series = series.loc[series < colMax]

                                stdDev = float(
                                    series.std(
                                        axis='index',
                                        skipna=True,
                                        level=None,
                                        ddof=1))

                                prepSqlItems[scaledCol] = \
                                    sqlStdScl(
                                        sqlItem=numColSqlItem,
                                        mean=numColNullFillValue,
                                        std=stdDev)

                                numOrigToPrepColMap[numCol] = \
                                    [scaledCol,

                                     dict(Nulls=numColNulls,
                                          NullFillValue=numColNullFillValue,
                                          Mean=numColNullFillValue,
                                          StdDev=stdDev)]

                            elif scaler == 'maxabs':
                                scaledCol = self._MAX_ABS_SCL_PREFIX + numCol + self._PREP_SUFFIX

                                maxAbs = float(max(abs(colMin), abs(colMax)))

                                prepSqlItems[scaledCol] = \
                                    sqlMaxAbsScl(
                                        sqlItem=numColSqlItem,
                                        maxAbs=maxAbs)

                                numOrigToPrepColMap[numCol] = \
                                    [scaledCol,

                                     dict(Nulls=numColNulls,
                                          NullFillValue=numColNullFillValue,
                                          MaxAbs=maxAbs)]

                            elif scaler == 'minmax':
                                scaledCol = self._MIN_MAX_SCL_PREFIX + numCol + self._PREP_SUFFIX

                                prepSqlItems[scaledCol] = \
                                    sqlMinMaxScl(
                                        sqlItem=numColSqlItem,
                                        origMin=colMin, origMax=colMax,
                                        targetMin=-1, targetMax=1)

                                numOrigToPrepColMap[numCol] = \
                                    [scaledCol,

                                     dict(Nulls=numColNulls,
                                          NullFillValue=numColNullFillValue,
                                          OrigMin=colMin, OrigMax=colMax,
                                          TargetMin=-1, TargetMax=1)]

                            else:
                                raise ValueError('*** Scaler must be one of "standard", "maxabs", "minmax" and None ***')

                        else:
                            scaledCol = self._NULL_FILL_PREFIX + numCol + self._PREP_SUFFIX

                            prepSqlItems[scaledCol] = numColSqlItem

                            numOrigToPrepColMap[numCol] = \
                                [scaledCol,

                                 dict(Nulls=numColNulls,
                                      NullFillValue=numColNullFillValue)]

                        numScaledCols.append(scaledCol)

                if verbose:
                    _toc = time.time()
                    self.stdout_logger.info(msg + ' done!   <{:,.1f} s>'.format(_toc - _tic))

            defaultVecCols = \
                [catOrigToPrepColMap[catCol][0]
                 for catCol in sorted(set(catOrigToPrepColMap)
                                      .difference(('__OHE__', '__SCALE__')))] + \
                [numOrigToPrepColMap[numCol][0]
                 for numCol in sorted(set(numOrigToPrepColMap)
                                      .difference(('__TS_WINDOW_CLAUSE__', '__SCALER__')))]

            sqlStatement = \
                'SELECT *, {} FROM __THIS__ {}'.format(
                    ', '.join('{} AS {}'.format(sqlItem, prepCol)
                              for prepCol, sqlItem in prepSqlItems.items()),
                    numNullFillDetails.get('__TS_WINDOW_CLAUSE__', ''))

        if savePath and (savePath != loadPath):
            if verbose:
                msg = 'Saving Data Transformations to Local Path "{}"...'.format(savePath)
                self.stdout_logger.info(msg)
                _tic = time.time()

            fs.mkdir(
                dir=savePath,
                hdfs=False)

            json.dump(
                catOrigToPrepColMap,
                open(os.path.join(savePath, self._CAT_ORIG_TO_PREP_COL_MAP_FILE_NAME), 'w'),
                indent=2)

            json.dump(
                numOrigToPrepColMap,
                open(os.path.join(savePath, self._NUM_ORIG_TO_PREP_COL_MAP_FILE_NAME), 'w'),
                indent=2)

            json.dump(
                sqlStatement,
                open(os.path.join(savePath, self._PREP_SQL_STATEMENT_FILE_NAME), 'w'),
                indent=2)

            if verbose:
                _toc = time.time()
                self.stdout_logger.info(msg + ' done!   <{:,.1f} s>'.format(_toc - _tic))

            self._PREP_CACHE[savePath] = \
                Namespace(
                    catOrigToPrepColMap=catOrigToPrepColMap,
                    numOrigToPrepColMap=numOrigToPrepColMap,
                    defaultVecCols=defaultVecCols,
                    
                    sqlStatement=sqlStatement,
                    sqlTransformer=None,
                    
                    catOHETransformer=None,
                    pipelineModelWithoutVectors=None)

        colsToKeep = \
            self.columns + \
            (([catPrepColDetails[0]
               for catCol, catPrepColDetails in catOrigToPrepColMap.items()
               if (catCol not in ('__OHE__', '__SCALE__')) and
                    isinstance(catPrepColDetails, list) and (len(catPrepColDetails) == 2)] +
              [numPrepColDetails[0]
               for numCol, numPrepColDetails in numOrigToPrepColMap.items()
               if (numCol not in ('__TS_WINDOW_CLAUSE__', '__SCALER__')) and
                    isinstance(numPrepColDetails, list) and (len(numPrepColDetails) == 2)])
             if loadPath
             else (((catScaledIdxCols
                     if scaleCat
                     else catIdxCols)
                    if catCols
                    else []) +
                   (numScaledCols
                    if numCols
                    else [])))

        missingCatCols = \
            set(catOrigToPrepColMap) \
            .difference(
                self.columns +
                ['__OHE__', '__SCALE__'])

        missingNumCols = \
            set(numOrigToPrepColMap) \
            .difference(
                self.columns +
                ['__TS_WINDOW_CLAUSE__', '__SCALER__'])

        missingCols = missingCatCols | missingNumCols

        addCols = {}

        if missingCols:
            if arimo.debug.ON:
                self.stdout_logger.debug(
                    msg='*** FILLING MISSING COLS {} ***'
                        .format())

            for missingCol in missingCols:
                addCols[missingCol] = \
                    numpy.nan \
                    if missingCol in missingCatCols \
                    else numOrigToPrepColMap[missingCol][1]['NullFillValue']

                colsToKeep.append(missingCol)

        arrowADF = \
            self.map(
                mapper=_ArrowADF__prep__pandasDFTransform(
                    addCols=addCols,
                    typeStrs=
                        {catCol: str(self.type(catCol))
                         for catCol in set(catOrigToPrepColMap).difference(('__OHE__', '__SCALE__'))},
                    catOrigToPrepColMap=catOrigToPrepColMap,
                    numOrigToPrepColMap=numOrigToPrepColMap,
                    _medianFill=_medianFill),
                inheritNRows=True,
                **kwargs)[colsToKeep]

        arrowADF._inheritCache(
            self,
            *(() if loadPath
                 else colsToKeep))

        arrowADF._cache.reprSample = self._cache.reprSample

        if verbose:
            toc = time.time()
            self.stdout_logger.info(message + ' done!   <{:,.1f} m>'.format((toc - tic) / 60))

        return ((arrowADF, catOrigToPrepColMap, numOrigToPrepColMap, sqlStatement)
                if returnSQLStatement
                else (arrowADF, catOrigToPrepColMap, numOrigToPrepColMap)) \
            if returnOrigToPrepColMaps \
          else arrowADF

    # *******************************
    # ITERATIVE GENERATION / SAMPLING

    def gen(self, *args, **kwargs):
        if self.fromS3:
            aws_access_key_id=self._srcArrowDS.fs.fs.key
            aws_secret_access_key=self._srcArrowDS.fs.fs.secret

        else:
            aws_access_key_id = aws_secret_access_key = None

        piecePaths = kwargs.get('piecePaths', self.piecePaths)

        return _ArrowADF__gen(
                args=args,
                piecePaths=piecePaths,
                aws_access_key_id=aws_access_key_id, aws_secret_access_key=aws_secret_access_key,
                partitionKVs={piecePath: self._PIECE_CACHES[piecePath].partitionKVs
                              for piecePath in piecePaths},
                iCol=self._iCol, tCol=self._tCol,
                possibleFeatureTAuxCols=self.possibleFeatureTAuxCols,
                contentCols=self.contentCols,
                pandasDFTransforms=self._mappers,
                filterConditions=kwargs.get('filter', {}),
                n=kwargs.get('n', 512),
                sampleN=kwargs.get('sampleN', 10 ** 5),
                anon=kwargs.get('anon', True),
                n_threads=kwargs.get('n_threads', 1))

    # ****
    # MISC
    # split

    def split(self, *weights, **kwargs):
        if (not weights) or weights == (1,):
            return self

        else:
            nWeights = len(weights)
            cumuWeights = numpy.cumsum(weights) / sum(weights)

            nPieces = self.nPieces

            piecePaths = list(self.piecePaths)
            random.shuffle(piecePaths)

            cumuIndices = \
                [0] + \
                [int(round(cumuWeights[i] * nPieces))
                 for i in range(nWeights)]

            return [self._subset(*piecePaths[cumuIndices[i]:cumuIndices[i + 1]], **kwargs)
                    for i in range(nWeights)]


class AthenaADF(_ADFABC):
    pass
