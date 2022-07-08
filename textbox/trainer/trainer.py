import os
import torch
import torch.optim as optim
import numpy as np
import math
import collections

from time import time
from tqdm import tqdm
from logging import getLogger

from textbox.trainer.scheduler import (
    AbstractScheduler, InverseSquareRootScheduler, CosineScheduler, LinearScheduler, ConstantScheduler
)
from textbox.evaluator import BaseEvaluator, evaluator_list
from textbox.utils import ensure_dir, get_local_time, init_seed
from textbox.utils.dashboard import get_dashboard, AbstractDashboard, TensorboardWriter, NilWriter

from typing import Dict, Optional, Union, Set, Collection, Literal, List, Tuple
from textbox.model.abstract_model import AbstractModel
from textbox.data import AbstractDataLoader
from textbox import Config
from logging import Logger
MetricsDict = Dict[str, Union[float, Dict[str, float], Collection[float]]]


class EpochTracker:
    r"""
    Track and visualize validating metrics results and training / validating loss.
    Dashboard now supports :class:`textbox.utils.TensorboardWriter`.

    Args:
        epoch_idx: Current epoch index.
        DDP: Whether the Pytorch DDP is enabled.
        train: Optional (default = True)
            Train or eval mode.
        dashboard: Optional (default = None)
            Implementation of visualization toolkit.

    Example:
        >>> tbw = TensorboardWriter("log/dir", "filename")
        >>> for epoch in range(10):
        >>>     valid_tracker = EpochTracker(epoch, DDP=False, train=False, dashboard=tbw)
        >>>     for step in range(10):
        >>>         valid_tracker.append_loss(1.0)
        >>>     valid_tracker.set_result({"metric1": 1.0})
        >>>     valid_tracker.info(time_duration=10.0)
        Epoch 0,  validating [time: 10.00s, validating_loss: 1.0000]
    """
    _is_train: Dict[bool, Literal["train", "valid"]] = {True: "train", False: "valid"}

    def __init__(
            self,
            epoch_idx: int,
            DDP: bool,
            train: bool = True,
            dashboard: Optional[AbstractDashboard] = None
    ):
        self.epoch_idx: int = epoch_idx

        self._accumulate_loss: float = 0.
        self._accumulate_step: int = 0
        self._loss: Optional[float] = None

        self.metrics_results: MetricsDict = dict()
        self._score: Optional[float] = None

        self._logger: Logger = getLogger()
        self._DDP: bool = DDP
        self.mode_tag = self._is_train[train]

        self._dashboard: AbstractDashboard = dashboard or NilWriter()
        self._dashboard.update_axes(self.mode_tag + "/epoch")

    def append_loss(self, loss: float):
        r"""Append loss of current step to tracker and update current step."""
        if math.isnan(loss):
            raise ValueError('Value is nan.')
        self._dashboard.add_scalar("loss/" + self.mode_tag, loss)
        self._dashboard.update_axes(self.mode_tag + "/step")

        self._accumulate_loss += loss
        self._accumulate_step += 1

    def set_result(self, results: dict):
        r"""Record the metrics results."""
        for metric, result in results.items():
            self._dashboard.add_any("metrics/" + metric, result)
        self.metrics_results.update(results)

    def _calc_score(self) -> Optional[float]:
        r"""Ensure the score will only be calculated and recorded once."""
        if self.metrics_results is None:
            return None

        # calculate the total score of valid metrics for early stopping.
        score: Optional[float] = None
        for metric, result in self.metrics_results.items():
            if isinstance(result, dict):
                float_list = list(filter(lambda x: isinstance(x, float), result.values()))
            elif isinstance(result, Collection):
                float_list = list(filter(lambda x: isinstance(x, float), result))
            elif isinstance(result, float):
                float_list = [result]
            else:
                self._logger.warning(f"Failed when working out score of metric {metric}.")
                continue
            if len(float_list) != 0:
                if score is None:
                    score = 0.
                score += sum(float_list) / len(float_list)
        if score is not None:
            self._dashboard.add_scalar("metrics/score", score)
        else:
            score = -self.loss  # If no valid metric is given, negative loss will be used in early stopping.

        return score

    @property
    def score(self) -> Optional[float]:
        r"""Get the total sum score of metric results in evaluating epochs."""
        if self.mode_tag == self._is_train[True]:
            self._logger.warning('`score` is unavailable training epochs.')
            return None

        if not self._score:
            self._score = self._calc_score()
        return self._score

    def _calc_loss(self) -> float:
        r"""Ensure the loss will only be calculated and reduced once if DDP is enabled."""
        if self._accumulate_step == 0:
            self._logger.warning("Trying to access epoch average loss before append any.")
            return math.inf
        _loss = self._accumulate_loss / self._accumulate_step
        if self._DDP:
            _loss = torch.tensor(_loss).to("cuda")
            torch.distributed.all_reduce(_loss, op=torch.distributed.ReduceOp.SUM)
            _loss /= torch.distributed.get_world_size()
            _loss = _loss.item()
        return _loss

    @property
    def loss(self) -> float:
        r"""Get total loss (loss will be reduced on the first call)."""
        if not self._loss:
            self._loss = self._calc_loss()
        return self._loss

    def as_dict(self) -> dict:
        return dict(
            epoch_idx=self.epoch_idx,
            metrics_results=self.metrics_results,
            loss=self.loss,
        )

    def info(self, time_duration: float, extra_info: str = ""):
        r"""Output loss with epoch and time information."""
        output = f"Epoch {self.epoch_idx}, {extra_info} {self.mode_tag} "\
                 f"[time: {time_duration:.2f}s, {self.mode_tag}_loss: {self.loss:.4f}"

        if self.mode_tag == "validating":
            for metric, result in self.metrics_results.items():
                if metric != 'loss':
                    output += f", {metric}: {result}"
        output += "]"

        # flush
        self._logger.info(output)

    def __getitem__(self, item: str) -> Optional[float]:
        if self.metrics_results is None:
            self._logger.warning("Please set result first.")
            return None
        return self.metrics_results[item]


class AbstractTrainer:
    r"""Trainer Class is used to manage the training and evaluation processes of text generation system models.
    AbstractTrainer is an abstract class in which the fit() and evaluate() method should be implemented according
    to different training and evaluation strategies.
    """

    def __init__(self, config: Config, model: AbstractModel):
        self.config = config
        self.model = model
        self.logger = getLogger()

    def fit(self, train_data: AbstractDataLoader):
        r"""Train the model based on the train data.
        """

        raise NotImplementedError('Method `fit()` should be implemented.')

    def evaluate(self, eval_data: AbstractDataLoader):
        r"""Evaluate the model based on the eval data.
        """

        raise NotImplementedError('Method `evaluate()` should be implemented.')


class Trainer(AbstractTrainer):
    r"""The basic Trainer for basic training and evaluation strategies in text generation systems.

    This class defines common functions for training and evaluation processes of most text generation system models,
    including `fit()`, `evaluate()`, `resume_checkpoint()` and some other features helpful for model training and
    evaluation.

    Generally speaking, this class can serve most text generation system models, If the training process of the model
    is to simply optimize a single loss without involving any complex training strategies, such as adversarial learning,
    pre-training and so on.

    Initializing the Trainer needs two parameters: `config` and `model`. `config` records the parameters' information
    for controlling training and evaluation, such as `learning_rate`, `epochs`, `eval_step` and so on.
    More information can be found in [placeholder]. `model` is the instantiated object of a Model Class.
    """

    def __init__(self, config: Config, model: AbstractModel):
        super(Trainer, self).__init__(config, model)

        self.DDP: bool = config['DDP']
        self.device: torch.device = config['device']
        self.filename = self.config['filename']

        self.stopping_step: Optional[int] = config['stopping_step']
        self.stopped = False
        self.stopping_count = 0
        self.init_lr: Optional[float] = config['init_lr']
        self.warmup_steps: Optional[int] = config['warmup_steps']
        self.max_steps: Optional[int] = config['max_steps']
        self.learning_rate: float = config['learning_rate']
        self.grad_clip: bool = config['grad_clip']
        self.optimizer = self._build_optimizer(config['optimizer'], config['scheduler'])

        ensure_dir(config['checkpoint_dir'])
        self.saved_model_filename: str = os.path.join(config['checkpoint_dir'], self.filename)
        r"""Path to saved checkpoint file, which can be loaded with `load_experiment`."""

        ensure_dir(config['generated_text_dir'])
        self.saved_text_filename: str = os.path.join(config['generated_text_dir'], self.filename)

        self.start_epoch = 0
        r"""Start epoch index. That is, `epoch_idx` iterates through `range(self.start_epoch, self.epochs)`"""
        self.epochs: int = config['epochs']
        r"""End epoch index + 1, aka max iteration times. That is, `epoch_idx` iterates through 
        `range(self.start_epoch, self.epochs)`"""
        self.epoch_idx: int = -1
        r"""current epoch index"""

        self.best_epoch = -1
        self.best_valid_score = -math.inf
        self.eval_interval, self.eval_mode = self._set_eval_mode()
        self._eval_count = 0
        self.train_loss_list: List[float] = list()
        self.result_list: List[dict] = list()

        self.metrics = self._process_metrics("metrics")
        self.evaluator = BaseEvaluator(config, self.metrics)
        self.valid_metrics = self._process_metrics("valid_metrics")
        self.valid_evaluator = BaseEvaluator(config, self.valid_metrics)

        self.disable_tqdm = self.DDP and torch.distributed.get_rank() != 0
        self.logdir = './log/'
        self._DashboardClass = get_dashboard(config['dashboard'], self.logdir, config)
        self._dashboard = None

    def _set_eval_mode(self) -> Tuple[int, Literal["epoch", "step"]]:
        r"""Check evaluation mode. Default = (1, "epoch") (If both `eval_epoch` and `eval_step` are specified,
        `eval_step` is ignored. If both are set to 0, `eval_epoch` is set to 1.)

        Returns:
            Tuple[int, Literal["epoch", "step"]]: a tuple of (evaluation interval, evaluation mode)
        """
        # default value
        eval_epoch = self.config['eval_epoch'] or 0
        eval_step = self.config['eval_step'] or 0

        # check
        if eval_epoch > 0 and eval_step > 0:
            self.logger.warning(
                '"eval_step" and "eval_epoch" are specified at the same time. "eval_step" has been ignored.'
            )
            eval_step = 0
        elif eval_epoch <= 0 and eval_step <= 0:
            self.logger.warning(
                '"eval_step" and "eval_epoch" are set to 0 at the same time. "eval_epoch" has been set to 1.'
            )
            eval_epoch = 1

        if eval_epoch > 0:
            self.logger.info(f"eval mode: validate every {eval_epoch} epoch")
            return eval_epoch, "epoch"
        else:
            self.logger.info(f"eval mode: validate every {eval_step} step")
        return eval_step, "step"

    def _build_optimizer(self, optimizer: Optional[str], scheduler: Optional[str])\
            -> Union[optim.Optimizer, AbstractScheduler]:
        """Init the optimizer and scheduler.

        Returns:
            Union[optim.Optimizer, AbstractScheduler]: the optimizer
        """

        def _get_base_optimizer(name: str) -> optim.Optimizer:

            name = name.lower()

            if name == 'adam':
                _optim = optim.Adam(self.model.parameters(), lr=self.learning_rate)
            elif name == 'sgd':
                _optim = optim.SGD(self.model.parameters(), lr=self.learning_rate)
            elif name == 'adagrad':
                _optim = optim.Adagrad(self.model.parameters(), lr=self.learning_rate)
            elif name == 'rmsprop':
                _optim = optim.RMSprop(self.model.parameters(), lr=self.learning_rate)
            else:
                self.logger.warning('Received unrecognized optimizer, set default Adam optimizer')
                _optim = optim.Adam(self.model.parameters(), lr=self.learning_rate)

            return _optim

        def _get_scheduler(name: Optional[str], _optim: optim.Optimizer) -> Union[optim.Optimizer, AbstractScheduler]:

            if name is None:
                return _optim

            name = name.lower()

            if name == 'inverse':
                _optim = InverseSquareRootScheduler(_optim, self.init_lr, self.learning_rate, self.warmup_steps)
            elif name == 'cosine':
                _optim = CosineScheduler(_optim, self.init_lr, self.learning_rate, self.warmup_steps, self.max_steps)
            elif name == 'linear':
                _optim = LinearScheduler(_optim, self.init_lr, self.learning_rate, self.warmup_steps, self.max_steps)
            elif name == 'constant':
                _optim = ConstantScheduler(_optim, self.init_lr, self.learning_rate, self.warmup_steps)
            else:
                self.logger.info("Learning rate scheduling disabled.")

            return _optim

        optimizer = _get_base_optimizer(optimizer)
        optimizer = _get_scheduler(scheduler, optimizer)
        return optimizer

    def _process_metrics(self, metrics_type: Literal["metrics", "valid_metrics"]) -> Set[str]:
        r"""Get and check the user-specific metrics. The function get metrics from config, checks if it's a string or
        a list, and then checks if the metrics are in the supported list.

        Args:
            metrics_type: "metrics" or "valid_metrics"

        Returns:
            Set[str]: A set of metrics. An empty set will be returned if none metric is specified.

        Raises:
            TypeError: If the `self.config[metrics_type]` is not a string or a list.
        """
        # Type check and pre-process
        metrics = self.config[metrics_type]
        if not metrics:
            return set()
        if not isinstance(metrics, (str, list)):
            raise TypeError(f'Evaluator(s) of {metrics_type} must be a string or list, not {type(metrics)}')
        if isinstance(metrics, str):
            if metrics[0] == '[' and metrics[-1] == ']':
                metrics = metrics[1:-1]
            metrics = metrics.split(",")
        metrics = set(map(lambda x: x.strip().lower(), metrics))

        # Implementation Check
        if len(metrics - evaluator_list) != 0:
            self.logger.warning(
                f"Evaluator(s) of {metrics_type} " + ", ".join(metrics - evaluator_list) +
                " are ignored because are not in supported evaluators list (" + ", ".join(evaluator_list) + ")."
            )
            metrics -= metrics - evaluator_list

        return metrics

    def _train_epoch(
            self,
            train_data: AbstractDataLoader,
            epoch_idx: int,
            valid_data: Optional[AbstractDataLoader] = None,
    ) -> EpochTracker:
        r"""Train the model in an epoch

        Args:
            train_data:
            epoch_idx: the current epoch index.
            valid_data: Optional (default = None) the dataloader of validation set

        Returns:
            EpochTracker: :class:`EpochTracker` that includes epoch information.
        """
        self.model.train()
        tracker = EpochTracker(epoch_idx, self.DDP, train=True, dashboard=self._dashboard)
        train_tqdm = tqdm(train_data, desc=f"train {epoch_idx:4}", ncols=100) if not self.disable_tqdm else train_data

        for data in train_tqdm:
            self.optimizer.zero_grad()

            loss = self.model(data, epoch_idx=epoch_idx)
            tracker.append_loss(loss)

            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.grad_clip)
            self.optimizer.step()

            if valid_data:
                self.stopped &= self._valid(valid_data, epoch_idx, 'step')
                if self.stopped:
                    break

        return tracker

    @torch.no_grad()
    def _valid_epoch(
            self,
            valid_data: AbstractDataLoader,
            epoch_idx: int,
    ) -> EpochTracker:
        r"""Valid the model with `valid_data`

        Args:
            valid_data: the dataloader of validation set
            epoch_idx: the current epoch index.

        Returns:
            EpochTracker: :class:`EpochTracker` that includes epoch information.
        """
        self.model.eval()
        tracker = EpochTracker(epoch_idx, self.DDP, train=False, dashboard=self._dashboard)
        valid_tqdm = tqdm(valid_data, desc=f"train {epoch_idx:4}", ncols=100) if not self.disable_tqdm else valid_data

        for data in valid_tqdm:
            losses = self.model(data)
            tracker.append_loss(losses)

        valid_results = self.evaluate(valid_data, load_best_model=False, is_valid=True)
        tracker.set_result(valid_results)

        return tracker

    def _valid(
            self,
            valid_data: AbstractDataLoader,
            epoch_idx: int,
            position: Literal["epoch", "step"],
    ) -> bool:
        """Validate every `self.eval_interval` step or epoch if invoke position matches attribute `self.eval_mode`.
        Specifically, if `self.eval_interval` is set to `0`, validation will be skipped.

        Early stopping will also be checked if `self.stopping_step` is positive integer.

        Args:
            valid_data: The dataloader of validation set.
            epoch_idx: The current epoch index.
            position: Invoke position of method ("epoch" or "step").

        Returns:
            bool: Early stopping. Return true if `self.stopping_step` is positive integer and `self._early_stopping()`
            is True.
        """
        if (position != self.eval_mode) or (self.eval_interval <= 0):
            return False

        self._eval_count += 1
        if self._eval_count % self.eval_interval != 0:
            return False

        start_time = time()
        valid_tracker = self._valid_epoch(valid_data, epoch_idx)
        end_time = time()
        valid_tracker.info(end_time - start_time, extra_info=str(self._eval_count // self.eval_interval))
        self.result_list.append(valid_tracker.as_dict())

        stopped = bool(self.stopping_step) and self._early_stopping(valid_tracker, epoch_idx)
        self.save_checkpoint()

        return stopped

    @property
    def best_result(self) -> dict:
        return self.result_list[self.best_epoch]

    def save_checkpoint(self, tag: Optional[str] = None, overwrite: bool = True):
        r"""Store the model parameters information and training information.

        Save checkpoint every validation as the formate of 'Model-Dataset-Time_epoch-?.pth'. A soft link named
        'Model-Dataset-Time.pth' pointing to best epoch will be created.

        Todo:
            * Large checkpoint
        """
        if len(self.result_list) == 0:
            self.logger.warning('Save checkpoint failed. No validation has been performed.')
            return

        # construct state_dict and parameters
        _state_dict = self.model.state_dict()
        if self.DDP and torch.distributed.get_rank() == 0:
            _new_dict = dict()
            for key, val in _state_dict["state_dict"].items():
                changed_key = key[7:] if key.startswith('module.') else key
                _new_dict[changed_key] = val
            _state_dict = _new_dict

        # get optimizer, config and validation summary
        checkpoint = {
            'state_dict': _state_dict,
            'optimizer': self.optimizer.state_dict(),
            'stopping_count': self.stopping_count,
            'best_valid_score': self.best_valid_score,
            'epoch': self.epoch_idx,
            'config': self.config,
            'summary': self.result_list[-1],
        }

        self.logger.debug(checkpoint)

        # deal with naming
        if tag is None:
            tag = '_epoch-' + str(self.epoch_idx)
        path_to_save = os.path.abspath(self.saved_model_filename + tag)
        if os.path.exists(path_to_save):
            if overwrite:
                os.remove(path_to_save)  # behavior of torch.save is not clearly defined.
            else:
                path_to_save += get_local_time()
        path_to_save += '.pth'

        torch.save(checkpoint, path_to_save)
        self.logger.info('Saving current: {}'.format(path_to_save))

        # create soft link to best model
        if self.best_epoch == self.epoch_idx:
            path_to_best = os.path.abspath(self.saved_model_filename + '.pth')
            if os.path.exists(path_to_best):
                os.remove(path_to_best)
            os.symlink(path_to_save, path_to_best)

    def _save_generated_text(self, generated_corpus: List[str]):
        r"""Store the generated text by our model into `self.saved_text_file`."""
        with open(self.saved_text_file + '.txt', 'w') as fout:
            for text in generated_corpus:
                fout.write(text + '\n')

    def resume_checkpoint(self, resume_file: str):
        r"""Load the model parameters information and training information.

        Args:
            resume_file: the checkpoint file (specific by `load_experiment`).
        """
        # check
        self.logger.info("Resuming checkpoint from {}...".format(resume_file))
        if os.path.isfile(resume_file):
            checkpoint = torch.load(resume_file, map_location=self.device)
        else:
            self.logger.warning('Checkpoint file "{}" not found. Resuming stopped.'.format(resume_file))
            return

        # load start epoch and early stopping
        self.start_epoch = checkpoint['epoch'] + 1
        self.stopping_count = checkpoint['stopping_count'] or checkpoint['cur_step']
        self.best_valid_score = checkpoint['best_valid_score']

        if checkpoint['config']['seed']:
            init_seed(checkpoint['config']['seed'], checkpoint['config']['reproducibility'])

        # load architecture params from checkpoint
        if checkpoint['config']['model'].lower() != self.config['model'].lower():
            self.logger.warning(
                'Architecture configuration given in config file is different from that of checkpoint. '
                'This may yield an exception while state_dict is being loaded.'
            )
        if self.DDP:
            saved_dict = collections.OrderedDict()
            for state_dict_key, state_dict_val in checkpoint['state_dict'].items():
                changed_key = 'module.' + state_dict_key
                saved_dict[changed_key] = state_dict_val
            checkpoint['state_dict'] = saved_dict
        self.model.load_state_dict(checkpoint['state_dict'])

        # load optimizer state from checkpoint only when optimizer type is not changed
        if checkpoint['config']['optimizer'].lower() != self.config['optimizer'].lower():
            self.logger.warning(
                'Optimizer configuration given in config file is different from that of checkpoint. '
                'This may yield an exception while state_dict is being loaded.'
            )
        self.optimizer.load_state_dict(checkpoint['optimizer'])
        self.logger.info('Checkpoint loaded. Resume training from epoch {}'.format(self.start_epoch))

    def fit(
            self,
            train_data: AbstractDataLoader,
            valid_data: Optional[AbstractDataLoader] = None,
    ) -> dict:
        r"""Train the model based on the train data and the valid data.

        Args:
            train_data: The dataloader of training set.
            valid_data: (default = None) The dataloader of training set.

        Returns:
             dict: the best valid score and best valid result.

        Todo:
            * Complete the docstring.
            * Modify the return value.
        """
        self.logger.info("====== Start training ======")
        with self._DashboardClass() as dashboard:
            self._dashboard = dashboard
            for epoch_idx in range(self.start_epoch, self.epochs):
                self.epoch_idx = epoch_idx
                # train
                start_time = time()
                train_tracker = self._train_epoch(train_data, epoch_idx, valid_data)
                self.train_loss_list.append(train_tracker.loss)
                end_time = time()
                train_tracker.info(end_time - start_time)

                # valid
                if valid_data:
                    self.stopped &= self._valid(valid_data, epoch_idx, 'epoch')
                    if self.stopped:
                        break

        self.logger.info('====== Finished training, best eval result in epoch {} ======'.format(self.best_epoch))

        return self.best_result

    def _early_stopping(self, valid_tracker: EpochTracker, epoch_idx: int) -> bool:
        r""" Return True if valid score has been lower than best score for `stopping_step` steps.

        Args:
            valid_tracker: contain valid information (loss and metrics score)

        Todo:
            * Abstract results.
        """

        stop_flag = False

        if valid_tracker.score > self.best_valid_score:
            self.best_valid_score = valid_tracker.score
            self.stopping_count = 0
            self.best_epoch = epoch_idx
        else:
            self.stopping_count += 1
            stop_flag = self.stopping_count > self.stopping_step

        return stop_flag

    @torch.no_grad()
    def evaluate(
            self,
            eval_data: AbstractDataLoader,
            load_best_model: bool = True,
            model_file: Optional[str] = None,
            _eval: bool = True,
            is_valid: bool = False
    ) -> Optional[dict]:
        r"""Evaluate the model based on the `eval_data`.

        Args:
            eval_data (AbstractDataLoader): the eval data
            load_best_model (bool, optional): whether load the best model in the training process, default: True.
                                              It should be set True, if users want to test the model after training.
            model_file (str, optional): the saved model file, default: None. If users want to test the previously
                                        trained model file, they can set this parameter.
            _eval: (default = True) True to evaluate and False to preview generation only.
            is_valid: (default = False) True if evaluate during validation

        Returns:
            dict: eval result, key is the eval metric and value in the corresponding metric value

        Todo:
            *
        """
        if is_valid:
            _evaluator = self.valid_evaluator
            _metrics = self.valid_metrics
            if load_best_model:
                load_best_model = False
                self.logger.warning('Evaluation should not load best model during validation. `load_best_model` has'
                                    'set to False temporarily.')
        else:
            _evaluator = self.evaluator
            _metrics = self.metrics

        if load_best_model:
            checkpoint_file = model_file or self.saved_model_filename
            if not os.path.isfile(checkpoint_file):
                self.logger.error(f'Failed to evaluate model: "{checkpoint_file}" not found.')
                return None
            self.logger.info('Loading model structure and parameters from {} ...'.format(checkpoint_file))
            checkpoint = torch.load(checkpoint_file, map_location=self.device)
            self.model.load_state_dict(checkpoint['state_dict'])

        self.model.eval()

        # preview only
        if not _eval or len(_metrics) == 0:
            generate_sentence = self.model.generate(next(eval_data), eval_data)
            generate_sentence = ' '.join(generate_sentence[0])
            self.logger.info('Generation Preview: ' + generate_sentence)
            return {"preview": generate_sentence}

        # generate
        generate_corpus = []
        eval_tqdm = tqdm(eval_data, desc="generating", ncols=100) if self.disable_tqdm else eval_data
        for batch_data in eval_tqdm:
            generated = self.model.generate(batch_data, eval_data)
            generate_corpus.extend(generated)
        self._save_generated_text(generate_corpus)
        reference_corpus = eval_data.dataset.target_text
        result = self.evaluator.evaluate(generate_corpus, reference_corpus)

        return result
