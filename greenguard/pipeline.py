# -*- coding: utf-8 -*-

import json
import logging
import os
from copy import deepcopy
from hashlib import md5

import cloudpickle
import numpy as np
from btb import BTBSession
from btb.tuning import Tunable
from mlblocks import MLPipeline
from mlblocks.discovery import load_pipeline
from sklearn.exceptions import NotFittedError
from sklearn.model_selection import KFold, StratifiedKFold

from greenguard.metrics import METRICS

LOGGER = logging.getLogger(__name__)


PIPELINES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), 'pipelines'))


def get_pipelines(pattern='', path=False, unstacked=False):
    """Get the list of available pipelines.

    Optionally filter the names using a patter or obtain
    the paths to the pipelines alongside their name.

    Args:
        pattern (str):
            Pattern to search for in the pipeline names
        path (bool):
            Whether to return a dictionary containing the pipeline
            paths instead of only a list with the names.
        unstacked (bool):
            Whether to load the pipelines that expect the readings
            to be already unstacked by signal_id. Defaults to ``False``.

    Return:
        list or dict:
            List of available and matching pipeline names.
            If `path=True`, return a dict containing the pipeline
            names as keys and their absolute paths as values.
    """
    pipelines = dict()
    pipelines_dir = PIPELINES_DIR
    if unstacked:
        pipelines_dir = os.path.join(pipelines_dir, 'unstacked')

    for filename in os.listdir(pipelines_dir):
        if filename.endswith('.json') and pattern in filename:
            name = os.path.basename(filename)[:-len('.json')]
            pipeline_path = os.path.join(PIPELINES_DIR, filename)
            pipelines[name] = pipeline_path

    if not path:
        pipelines = list(pipelines)

    return pipelines


class GreenGuardPipeline(object):
    """Main Machine Learning component in the GreenGuard project.

    The ``GreenGuardPipeline`` represents the abstraction of a Machine
    Learning pipeline architecture specialized on the GreenGuard data
    format.

    In order to use it, an MLBlocks pipeline template needs to be given,
    alongside information about how to evaluate its performance using
    cross validation.

    Attributes:
        template (MLPipeline):
            MLPipeline instance used as the template for tuning.
        template_name:
            Name of the template being used.
        fitted (bool):
            Whether this GreenGuardPipeline has already been fitted or not.
        steps (list):
            List of primitives that compose this template.
        preprocessing (list):
            List of preprocessing steps. These steps have no learning stage
            and are executed only once on the complete training dataset, before
            partitioning it for cross validation.
        static (list):
            List of static steps. These are all the steps in the pipeline that
            come after the preprocessing ones but have no hyperparameters.
            These are executed on each cross validation split only once, when
            the data is partitioned, and their output is cached to be reused
            later on at every tuning iteration.
        tunable (list):
            List of steps that have hyperparameters and will be tuned during
            the tuning loop.

    Args:
        template (str or MLPipeline):
            Template to use. If a ``str`` is given, load the corresponding
            ``MLPipeline``.
        metric (str or function):
            Metric to use. If an ``str`` is give it must be one of the metrics
            defined in the ``greenguard.metrics.METRICS`` dictionary.
        cost (bool):
            Whether the metric is a cost function (the lower the better) or not.
            Defaults to ``False``.
        init_params (dict):
            Initial parameters to pass to the underlying MLPipeline if something
            other than the defaults need to be used.
            Defaults to ``None``.
        stratify (bool):
            Whether to stratify the data when partitioning for cross validation.
            Defaults to ``True``.
        cv_splits (int):
            Number of cross validation folds to use. Defaults to ``5``.
        shuffle (bool):
            Whether to shuffle the data when partitioning for cross validation.
            Defaults to ``True``.
        random_state (int or RandomState):
            random state to use for the cross validation partitioning.
            Defaults to ``0``.
        preprocessing (int):
            Number of steps to execute during the preprocessing stage.
            The number of preprocessing steps cannot be higher than the
            number of static steps in the given template.
            Defaults to ``0``.
    """

    template = None
    template_name = None
    fitted = False
    cv_score = None

    _cv_class = None
    _metric = None
    _cost = False
    _tuner = None
    _pipeline = None
    _static = None
    _init_params = None
    _preprocessing = None

    def _get_cv(self, stratify, cv_splits, shuffle, random_state):
        if stratify:
            cv_class = StratifiedKFold
        else:
            cv_class = KFold

        return cv_class(n_splits=cv_splits, shuffle=shuffle, random_state=random_state)

    @staticmethod
    def _count_static_steps(pipeline):
        tunable_hyperparams = pipeline.get_tunable_hyperparameters()
        for index, block_name in enumerate(pipeline.blocks.keys()):
            if tunable_hyperparams[block_name]:
                return index

        return 0

    @staticmethod
    def _update_params(old, new):
        for name, params in new.items():
            if '#' not in name:
                name = name + '#1'

            block_params = old.setdefault(name, dict())
            for param, value in params.items():
                block_params[param] = value

    @staticmethod
    def _get_templates(template):
        if not isinstance(template, list):
            templates = [template]
        else:
            templates = template

        templates_dict = dict()
        for template in templates:
            if isinstance(template, str):
                template_name = template
                template = load_pipeline(template_name)
            else:
                template_name = md5(json.dumps(template)).digest()

            templates_dict[template_name] = template

        return templates_dict

    def _get_init_params(self, template_name):
        if self._init_params is None:
            return {}

        elif template_name in self._init_params:
            return self._init_params.get(template_name)

        return self._init_params

    def _get_preprocessing(self, template_name):
        if isinstance(self._preprocessing, int):
            return self._preprocessing

        if isinstance(self._preprocessing, dict):
            return self._preprocessing.get(template_name) or 0

        return 0  # by default

    def _build_pipeline(self, hyperparameters=None):
        template_params = self.template.setdefault('init_params', dict())
        for name, params in list(template_params.items()):
            if '#' not in name:
                template_params[name + '#1'] = template_params.pop(name)

        init_params = self._get_init_params(self.template_name)

        if init_params:
            self._update_params(template_params, init_params)

        self._pipeline = MLPipeline(self.template_name)

        if hyperparameters:
            self._pipeline.set_hyperparameters(hyperparameters)

        self.fitted = False

    def __init__(self, template, metric='accuracy', cost=False, init_params=None, stratify=True,
                 cv_splits=5, shuffle=True, random_state=0, preprocessing=0):

        self._cv = self._get_cv(stratify, cv_splits, shuffle, random_state)

        if isinstance(metric, str):
            metric, cost = METRICS[metric]

        self._metric = metric
        self._cost = cost
        self._init_params = init_params
        self._preprocessing = preprocessing

        self.cv_score = np.inf if cost else -np.inf

        self.templates = self._get_templates(template)
        self.template_name = list(self.templates.keys())[0]
        self.template = self.templates[self.template_name]

        self._build_pipeline()

        _static = self._count_static_steps(self._pipeline)
        _preprocessing = self._get_preprocessing(self.template_name)
        if _preprocessing and (_preprocessing > _static):
            raise ValueError('Preprocessing cannot be bigger than static')

        self.iterations = 0

    def __repr__(self):
        steps = self._pipeline.primitives.copy()
        preprocessing = self._get_preprocessing(self.template_name)
        static = self._count_static_steps()

        preprocessing_steps = steps[:preprocessing]
        static_steps = steps[preprocessing:static]
        tunable_steps = steps[static:]

        return (
            "GreenGuardPipeline({})\n"
            "  preprocessing:\n{}\n"
            "  static:\n{}\n"
            "  tunable:\n{}\n"
        ).format(
            self.template_name,
            '\n'.join('    {}'.format(step) for step in preprocessing_steps),
            '\n'.join('    {}'.format(step) for step in static_steps),
            '\n'.join('    {}'.format(step) for step in tunable_steps),
        )

    def get_hyperparameters(self):
        """Get the current hyperparameters.

        Returns:
            dict:
                Current hyperparameters.
        """
        return deepcopy(self._hyperparameters)

    @staticmethod
    def _clone_pipeline(pipeline):
        return MLPipeline.from_dict(pipeline.to_dict())

    def _is_better(self, score):
        if self._cost:
            return score < self.cv_score

        return score > self.cv_score

    def _generate_splits(self, template_name, target_times, readings, turbines=None):
        template = self.templates.get(template_name)

        X = target_times[['turbine_id', 'cutoff_time']]
        y = target_times['target']

        pipeline = MLPipeline(template)
        preprocessing = self._get_preprocessing(template_name)
        static = self._count_static_steps(pipeline)

        if preprocessing:
            LOGGER.debug('Running %s preprocessing steps', preprocessing)
            context = pipeline.fit(X=X, y=y, readings=readings,
                                   turbines=turbines, output_=preprocessing - 1)
            del context['X']
            del context['y']
        else:
            context = {
                'readings': readings,
                'turbines': turbines,
            }

        splits = list()
        try:
            for fold, (train_index, test_index) in enumerate(self._cv.split(X, y)):
                LOGGER.debug('Running static steps for fold %s', fold)
                X_train, X_test = X.iloc[train_index], X.iloc[test_index]
                y_train, y_test = y.iloc[train_index], y.iloc[test_index]

                pipeline = MLPipeline(template)
                fit = pipeline.fit(X_train, y_train, output_=static - 1,
                                   start_=preprocessing, **context)
                predict = pipeline.predict(X_test, output_=static - 1,
                                           start_=preprocessing, **context)

                splits.append((fold, pipeline, fit, predict, y_test, static))

        except Exception:
            LOGGER.info('Could not generate splits for %', template_name)

        return splits

    def cross_validate(self, template_name, template_splits, params=None):
        """Compute cross validation score using the given data.

        If the splits have not been previously computed, compute them now.
        During this computation, the data is partitioned using the indicated
        cross validation parameters and later on processed using the
        pipeline static steps.

        The results of the fit and produce executions are cached and reused
        in subsequent calls to this method.

        Args:
            X (pandas.DataFrame):
                ``target_times`` data, without the ``target`` column.
                Only needed if the splits have not been previously computed.
            y (pandas.Series or numpy.ndarray):
                ``target`` vector corresponding to the passed ``target_times``.
                Only needed if the splits have not been previously computed.
            readings (pandas.DataFrame):
                ``readings`` table. Only needed if the splits have not been
                previously computed.
            turbines (pandas.DataFrame):
                ``turbines`` table. Only needed if the splits have not been
                previously computed.
            params (dict):
                hyperparameter values to use.

        Returns:
            float:
                Computed cross validation score. This score is the average
                of the scores obtained accross all the cross validation folds.
        """
        scores = []

        for fold, pipeline, fit, predict, y_test, static in template_splits:
            LOGGER.debug('Scoring fold %s', fold)

            pipeline.set_hyperparameters(params)

            pipeline.fit(start_=static, **fit)
            predictions = pipeline.predict(start_=static, **predict)

            score = self._metric(y_test, predictions)

            LOGGER.debug('Fold fold %s score: %s', fold, score)
            scores.append(score)

        cv_score = np.mean(scores)

        if self._is_better(cv_score):
            self.cv_score = cv_score
            self.template_name = template_name
            self._hyperparameters = deepcopy(params)
            self._build_pipeline(self._hyperparameters)

        return cv_score

    @staticmethod
    def _parse_params(param_details):
        param_type = param_details['type']
        param_details['type'] = 'str' if param_type == 'string' else param_type

        if param_details['type'] == 'bool':
            param_details['range'] = [True, False]
        else:
            param_details['range'] = param_details.get('range') or param_details.get('values')

        if 'default' not in param_details:
            param_details['default'] = param_details['range'][0]

        return param_details

    @classmethod
    def _get_tunables(cls, templates):
        pipelines = {name: MLPipeline(template) for name, template in templates.items()}
        tunables = {}

        for pipeline_name, pipeline in pipelines.items():
            pipeline_tunables = {}
            for name, param_details in pipeline.get_tunable_hyperparameters(flat=True).items():
                pipeline_tunables[name] = cls._parse_params(param_details)

            tunables[pipeline_name] = Tunable.from_dict(pipeline_tunables)

        return tunables

    def _make_btb_scorer(self, target_times, readings, turbines):

        splits = {
            template_name: self._generate_splits(template_name, target_times, readings, turbines)
            for template_name in list(self.templates.keys())
        }

        def scorer(template_name, config):
            template_splits = splits.get(template_name)
            if template_splits:
                score = self.cross_validate(template_name, template_splits, config)

            else:
                return None

            return score

        return scorer

    def tune(self, target_times, readings, turbines=None, iterations=10):
        """Tune this pipeline for the indicated number of iterations.

        Args:
            target_times (pandas.DataFrame):
                ``target_times`` table, containing the ``turbine_id``, ``cutoff_time``
                and ``target`` columns.
                Only needed if the splits have not been previously computed.
            readings (pandas.DataFrame):
                ``readings`` table. Only needed if the splits have not been
                previously computed.
            turbines (pandas.DataFrame):
                ``turbines`` table. Only needed if the splits have not been
                previously computed.
            iterations (int):
                Number of iterations to perform.
        """
        scoring_function = self._make_btb_scorer(target_times, readings, turbines)
        tunables = self._get_tunables(self.templates)
        session = BTBSession(tunables, scoring_function, maximize=not self._cost)
        if iterations:
            session.run(iterations)

        return session

    def fit(self, target_times, readings, turbines=None):
        """Fit this pipeline to the given data.

        Args:
            target_times (pandas.DataFrame):
                ``target_times`` table, containing the ``turbine_id``, ``cutoff_time``
                and ``target`` columns.
            readings (pandas.DataFrame):
                ``readings`` table.
            turbines (pandas.DataFrame):
                ``turbines`` table.
        """
        X = target_times[['turbine_id', 'cutoff_time']]
        y = target_times['target']
        self._pipeline.fit(X, y, readings=readings, turbines=turbines)
        self.fitted = True

    def predict(self, target_times, readings, turbines=None):
        """Make predictions using this pipeline.

        Args:
            target_times (pandas.DataFrame):
                ``target_times`` table, containing the ``turbine_id``, ``cutoff_time``
                and ``target`` columns.
            readings (pandas.DataFrame):
                ``readings`` table.
            turbines (pandas.DataFrame):
                ``turbines`` table.

        Returns:
            numpy.ndarray:
                Vector of predictions.
        """
        if not self.fitted:
            raise NotFittedError()

        X = target_times[['turbine_id', 'cutoff_time']]
        return self._pipeline.predict(X, readings=readings, turbines=turbines)

    def save(self, path):
        """Serialize and save this pipeline using cloudpickle.

        Args:
            path (str):
                Path to the file where the pipeline will be saved.
        """
        with open(path, 'wb') as pickle_file:
            cloudpickle.dump(self, pickle_file)

    @classmethod
    def load(cls, path):
        """Load a previously saved pipeline from a file.

        Args:
            path (str):
                Path to the file where the pipeline is saved.

        Returns:
            GreenGuardPipeline:
                Loaded GreenGuardPipeline instance.
        """
        with open(path, 'rb') as pickle_file:
            return cloudpickle.load(pickle_file)
