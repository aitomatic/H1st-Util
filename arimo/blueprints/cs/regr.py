from __future__ import absolute_import, division

import copy
import math
import os
import pandas
import psutil
import random

import arimo.backend
from arimo.blueprints.base import _docstr_blueprint
from arimo.blueprints.cs import _DLCrossSectSupervisedBlueprintABC
from arimo.df.spark_on_files import FileADF
from arimo.util import fs, Namespace
from arimo.util.decor import _docstr_verbose
from arimo.util.pkl import pickle_able
import arimo.debug

from ..mixins.eval import RegrEvalMixIn


@_docstr_blueprint
class DLBlueprint(RegrEvalMixIn, _DLCrossSectSupervisedBlueprintABC):
    _DEFAULT_PARAMS = \
        copy.deepcopy(
            _DLCrossSectSupervisedBlueprintABC._DEFAULT_PARAMS)

    _DEFAULT_PARAMS.update(
        model=Namespace(
            factory=Namespace(
                name='arimo.dl.experimental.keras.simple_crosssect_fdfwd_regressor'),

            train=Namespace(
                objective=None   # *** DON'T IMPOSE MSE UP-FRONT AS IT'S OVER-SENSITIVE TO LARGE OUTLIERS ***
            )),

        __metadata__={
            'model': Namespace(
                label='Model Params'),

            'model.factory': Namespace(
                label='Model-Initializing Factory Function Name & Params')})

    @_docstr_verbose
    def train(self, *args, **kwargs):
        __gen_queue_size__ = \
            kwargs.pop(
                '__gen_queue_size__',
                self.DEFAULT_MODEL_TRAIN_MAX_GEN_QUEUE_SIZE)

        __n_workers__ = \
            kwargs.pop(
                '__n_workers__',
                self.DEFAULT_MODEL_TRAIN_N_WORKERS)

        assert __n_workers__, '*** __n_workers__ = {} ***'.format(__n_workers__)

        __multiproc__ = kwargs.pop('__multiproc__', True)

        # verbosity
        verbose = kwargs.pop('verbose', True)

        # get ADF & model
        adf, model = \
            self.prep_data(
                __mode__=self._TRAIN_MODE,
                verbose=verbose,
                *args, **kwargs)

        if arimo.debug.ON:
            model.stdout_logger.debug(
                '*** TRAINING ON {} ({} ROWS) ***'
                    .format(adf, adf.nRows))

        # whether to exclude outlying labels
        _lower_outlier_threshold_applicable = \
            pandas.notnull(self.params.data.label.lower_outlier_threshold)

        _upper_outlier_threshold_applicable = \
            pandas.notnull(self.params.data.label.upper_outlier_threshold)

        __excl_outliers__ = \
            self.params.data.label.excl_outliers and \
            self.params.data.label.outlier_tails and \
            self.params.data.label.outlier_tail_proportion and \
            (self.params.data.label.outlier_tail_proportion < .5) and \
            (_lower_outlier_threshold_applicable or _upper_outlier_threshold_applicable)

        if __excl_outliers__:
            _outlier_robust_condition = \
                ('BETWEEN {} AND {}'
                    .format(
                        self.params.data.label.lower_outlier_threshold,
                        self.params.data.label.upper_outlier_threshold)
                 if _upper_outlier_threshold_applicable
                 else '>= {}'.format(self.params.data.label.lower_outlier_threshold)) \
                if _lower_outlier_threshold_applicable \
                else '<= {}'.format(self.params.data.label.upper_outlier_threshold)

            if arimo.debug.ON:
                model.stdout_logger.debug(
                    msg='*** TRAIN: CONDITION ROBUST TO OUTLIER LABELS: {} {} ***'
                        .format(self.params.data.label.var, _outlier_robust_condition))

        self._derive_model_train_params(
            data_size=
                adf.nRows
                if self.params.model.train.n_samples_max_multiple_of_data_size
                else None)

        model.stdout_logger.info(
            'TRAINING:'
            '\n- Predictor Variables Vector Size: {:,}'
            '\n- No. of Train Samples: {:,}'
            '\n- No. of Validation Samples: {:,}'
            '\n- No. of Epochs: {:,}'
            '\n- No. of Train Samples per Epoch: {:,}'
            '\n- No. of Validation Samples per Epoch: {:,}'
            '\n- Generator Queue Size: {}'
            '\n- No. of Processes/Threads: {}'
            '\n- Multi-Processing: {}'
            .format(
                self.params.data._prep_vec_size,
                self.params.model.train._n_train_samples,
                self.params.model.train._n_val_samples,
                self.params.model.train._n_epochs,
                self.params.model.train._n_train_samples_per_epoch,
                self.params.model.train._n_val_samples_per_epoch,
                __gen_queue_size__,
                __n_workers__,
                __multiproc__))

        fs.mkdir(
            dir=model.dir,
            hdfs=False)

        open(os.path.join(
                model.dir,
                self.params.model._persist.struct_file), 'w') \
            .write(model.to_json())

        assert isinstance(adf, FileADF)
        piece_sub_paths = list(adf.pieceSubPaths)
        random.shuffle(piece_sub_paths)
        split_idx = int(math.ceil(self.params.model.train.train_proportion * adf.nPieces))

        n_threads = int(math.ceil(psutil.cpu_count(logical=True) / __n_workers__))

        train_gen = \
            adf.gen(
                self.params.data._cat_prep_cols + self.params.data._num_prep_cols,
                self.params.data.label.var,
                pieceSubPaths=piece_sub_paths[:split_idx],
                n=self.params.model.train.batch_size,
                withReplacement=False,
                seed=None,
                anon=True,
                collect='numpy',
                pad=None,
                cache=False,
                filter={self.params.data.label.var: (self.params.data.label.lower_outlier_threshold,
                                                     self.params.data.label.upper_outlier_threshold)}
                    if __excl_outliers__
                    else {},
                n_threads=n_threads)

        assert pickle_able(train_gen)

        val_gen = \
            adf.gen(
                self.params.data._cat_prep_cols + self.params.data._num_prep_cols,
                self.params.data.label.var,
                pieceSubPaths=piece_sub_paths[split_idx:],
                n=self.params.model.train.val_batch_size,
                withReplacement=False,
                seed=None,
                anon=True,
                collect='numpy',
                pad=None,
                cache=False,
                filter={self.params.data.label.var: (self.params.data.label.lower_outlier_threshold,
                                                     self.params.data.label.upper_outlier_threshold)}
                    if __excl_outliers__
                    else {},
                n_threads=n_threads)

        assert pickle_able(val_gen)

        model.history = \
            model.fit_generator(
                generator=train_gen(),
                    # a generator.
                    # The output of the generator must be either a tuple(inputs, targets)
                    # or a tuple(inputs, targets, sample_weights).
                    # Therefore, all arrays in this tuple must have the same length (equal to the size of this batch).
                    # Different batches may have different sizes.
                    # For example, the last batch of the epoch is commonly smaller than the others,
                    # if the size of the dataset is not divisible by the batch size.
                    # The generator is expected to loop over its data indefinitely.
                    # An epoch finishes when steps_per_epoch batches have been seen by the model.

                steps_per_epoch=self.params.model.train._n_train_batches_per_epoch,
                    # Total number of steps (batches of samples) to yield from generator
                    # before declaring one epoch finished and starting the next epoch.
                    # It should typically be equal to the number of samples of your dataset divided by the batch size.

                epochs=self.params.model.train._n_epochs,
                    # Integer, total number of iterations on the data.
                    # Note that in conjunction with initial_epoch, the parameter epochs is to be understood as "final epoch".
                    # The model is not trained for n steps given by epochs, but until the epoch epochs is reached.

                verbose=2,
                    # Integer. 0, 1, or 2. Verbosity mode. 0 = silent, 1 = progress bar, 2 = one line per epoch.

                callbacks=[   # list of callbacks to be called during training.
                    # BaseLogger(stateful_metrics=None),
                        # Callback that accumulates epoch averages of metrics.
                        # This callback is automatically applied to every Keras model.

                    # *** DISABLED BECAUSE PROGRESS IS ALREADY DISPLAYED WHEN verbose = True ***
                    # ProgbarLogger(count_mode='samples', stateful_metrics=None),
                        # Callback that prints metrics to stdout.

                    # History(),
                        # Callback that records events into a History object.
                        # This callback is automatically applied to every Keras model.
                        # The History object gets returned by the fit method of models.

                    arimo.backend.keras.callbacks.TerminateOnNaN(),
                        # Callback that terminates training when a NaN loss is encountered.

                    arimo.backend.keras.callbacks.ModelCheckpoint(
                        # Save the model after every epoch.

                        filepath=os.path.join(model.dir, self.params.model._persist.weights_file),
                            # string, path to save the model file.

                        monitor=self.params.model.train.val_metric.name,
                            # quantity to monitor.

                        verbose=int(verbose > 0),
                            # verbosity mode, 0 or 1

                        save_best_only=(self.params.model.train.min_n_val_samples_per_epoch == 'all'),
                            # if save_best_only=True,
                            # the latest best model according to the quantity monitored will not be overwritten.

                        save_weights_only=True,
                            # if True, then only the model's weights will be saved (model.save_weights(filepath)),
                            # else the full model is saved (model.save(filepath))

                        mode=self.params.model.train.val_metric.mode,
                            # one of {auto, min, max}.
                            # If save_best_only=True, the decision to overwrite the current save file is made
                            # based on either the maximization or the minimization of the monitored quantity.
                            # For val_acc, this should be max, for val_loss this should be min, etc.
                            # In auto mode, the direction is automatically inferred from the name of the monitored quantity.

                        period=1
                            # Interval (number of epochs) between checkpoints.
                        ),

                    arimo.backend.keras.callbacks.ReduceLROnPlateau(
                        # Reduce learning rate when a metric has stopped improving.
                        # Models often benefit from reducing the learning rate by a factor of 2-10 once learning stagnates.
                        # This callback monitors a quantity and if no improvement is seen for a 'patience' number of epochs,
                        # the learning rate is reduced.

                        monitor=self.params.model.train.val_metric.name,
                            # quantity to be monitored.

                        factor=self.params.model.train.reduce_lr_on_plateau.factor,
                            # factor by which the learning rate will be reduced. new_lr = lr * factor

                        patience=self.params.model.train.reduce_lr_on_plateau.patience_n_epochs,
                            # number of epochs with no improvement after which learning rate will be reduced.

                        verbose=int(verbose > 0),
                            # int. 0: quiet, 1: update messages.

                        mode=self.params.model.train.val_metric.mode,
                            # one of {auto, min, max}.
                            # In min mode, lr will be reduced when the quantity monitored has stopped decreasing;
                            # In max mode it will be reduced when the quantity monitored has stopped increasing;
                            # In auto mode, the direction is automatically inferred from the name of the monitored quantity.

                        epsilon=self.params.model.train.val_metric.significance,
                            # threshold for measuring the new optimum, to only focus on significant changes.

                        cooldown=0,
                            # number of epochs to wait before resuming normal operation after lr has been reduced.

                        min_lr=0
                            # lower bound on the learning rate.
                        ),

                    arimo.backend.keras.callbacks.EarlyStopping(
                        # Stop training when a monitored quantity has stopped improving.

                        monitor=self.params.model.train.val_metric.name,
                            # quantity to be monitored.

                        min_delta=self.params.model.train.val_metric.significance,
                            # minimum change in the monitored quantity to qualify as an improvement,
                            # i.e. an absolute change of less than min_delta, will count as no improvement.

                        patience=max(self.params.model.train.early_stop.patience_min_n_epochs,
                                     int(math.ceil(self.params.model.train.early_stop.patience_proportion_total_n_epochs *
                                                  self.params.model.train._n_epochs))),
                            # number of epochs with no improvement after which training will be stopped.

                        verbose=int(verbose),
                            # verbosity mode.

                        mode=self.params.model.train.val_metric.mode
                            # one of {auto, min, max}.
                            # In min mode, training will stop when the quantity monitored has stopped decreasing;
                            # In max mode it will stop when the quantity monitored has stopped increasing;
                            # In auto mode, the direction is automatically inferred from the name of the monitored quantity.
                        )],

                validation_data=val_gen(),
                    # this can be either:
                    # - a generator for the validation data;
                    # - a tuple(inputs, targets); or
                    # - a tuple(inputs, targets, sample_weights).

                validation_steps=self.params.model.train._n_val_batches_per_epoch,
                    # Only relevant if validation_data is a generator.
                    # Total number of steps (batches of samples) to yield from  validation_data generator
                    # before stopping at the end of every epoch.
                    # It should typically be equal to the number of samples of your validation dataset divided by the batch size.

                class_weight={},
                    # Optional dictionary mapping class indices (integers) to a weight (float) value,
                    # used for weighting the loss function (during training only).
                    # This can be useful to tell the model to "pay more attention" to samples from an under-represented class.

                max_queue_size=__gen_queue_size__,
                    # Integer. Maximum size for the generator queue.
                    # If unspecified, max_queue_size will default to 10

                workers=__n_workers__,
                    # Integer. Maximum number of processes to spin up when using process-based threading.
                    # If unspecified, workers will default to 1. If 0, will execute the generator on the main thread.

                use_multiprocessing=__multiproc__,
                    # Boolean. If True, use process-based threading.
                    # If unspecified, use_multiprocessing will default to False.
                    # Note that because this implementation relies on multiprocessing,
                    # you should not pass non-picklable arguments to the generator
                    # as they can't be passed easily to children processes.

                shuffle=False,
                    # Boolean (whether to shuffle the order of the batches at the beginning of each epoch
                    # Only used with instances of Sequence (keras.utils.Sequence).
                    # Has no effect when steps_per_epoch is not None.

                initial_epoch=0
                    # epoch at which to start training (useful for resuming a previous training run)
                ) \
            .history

        model.save()

        return model
