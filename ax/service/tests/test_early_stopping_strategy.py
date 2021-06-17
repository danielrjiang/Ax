#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from ax.service.early_stopping_strategy import BaseEarlyStoppingStrategy
from ax.service.early_stopping_strategy import PercentileEarlyStoppingStrategy
from ax.utils.common.testutils import TestCase
from ax.utils.testing.core_stubs import (
    get_branin_arms,
    get_branin_experiment,
    get_branin_experiment_with_timestamp_map_metric,
)


class TestEarlyStoppingStrategy(TestCase):
    def test_early_stopping_strategy(self):
        # can't instantiate abstract class
        with self.assertRaises(TypeError):
            BaseEarlyStoppingStrategy()

    def test_percentile_early_stopping_strategy_validation(self):
        exp = get_branin_experiment()

        for i in range(5):
            trial = exp.new_trial().add_arm(arm=get_branin_arms(n=1, seed=i)[0])
            trial.run()

        early_stopping_strategy = PercentileEarlyStoppingStrategy()
        idcs = set(exp.trials.keys())
        exp.attach_data(data=exp.fetch_data())

        # Non-MapData attached
        with self.assertRaises(ValueError):
            early_stopping_strategy.should_stop_trials_early(
                trial_indices=idcs, experiment=exp
            )

        exp = get_branin_experiment_with_timestamp_map_metric(rate=0.5)
        for i in range(5):
            trial = exp.new_trial().add_arm(arm=get_branin_arms(n=1, seed=i)[0])
            trial.run()

        # No data attached
        with self.assertRaises(ValueError):
            early_stopping_strategy.should_stop_trials_early(
                trial_indices=idcs, experiment=exp
            )

        exp.attach_data(data=exp.fetch_data())

        # Not enough learning curves
        early_stopping_strategy = PercentileEarlyStoppingStrategy(
            min_curves=6,
        )
        should_stop = early_stopping_strategy.should_stop_trials_early(
            trial_indices=idcs, experiment=exp
        )
        self.assertEqual(should_stop, set())

        # Most recent fidelity below minimum
        early_stopping_strategy = PercentileEarlyStoppingStrategy(
            min_progression=3,
        )
        should_stop = early_stopping_strategy.should_stop_trials_early(
            trial_indices=idcs, experiment=exp
        )
        self.assertEqual(should_stop, set())

    def test_percentile_early_stopping_strategy(self):
        exp = get_branin_experiment_with_timestamp_map_metric(rate=0.5)
        for i in range(5):
            trial = exp.new_trial().add_arm(arm=get_branin_arms(n=1, seed=i)[0])
            trial.run()

        exp.attach_data(data=exp.fetch_data())

        """
        Data looks like this:
        arm_name metric_name        mean  sem  trial_index  timestamp
        0       0_0      branin  146.138620  0.0            0          0
        1       0_0      branin  117.388086  0.0            0          1
        2       0_0      branin   99.950007  0.0            0          2
        3       1_0      branin  113.057480  0.0            1          0
        4       1_0      branin   90.815154  0.0            1          1
        5       1_0      branin   77.324501  0.0            1          2
        6       2_0      branin   44.627226  0.0            2          0
        7       2_0      branin   35.847504  0.0            2          1
        8       2_0      branin   30.522333  0.0            2          2
        9       3_0      branin  143.375669  0.0            3          0
        10      3_0      branin  115.168704  0.0            3          1
        11      3_0      branin   98.060315  0.0            3          2
        12      4_0      branin   65.033535  0.0            4          0
        13      4_0      branin   52.239184  0.0            4          1
        14      4_0      branin   44.479018  0.0            4          2

        Looking at the most recent fidelity only (timestamp==2), we have
        the following metric values for each trial:
        0: 99.950007 <-- worst
        3: 98.060315
        1: 77.324501
        4: 44.479018
        2: 30.522333 <-- best
        """
        idcs = set(exp.trials.keys())

        early_stopping_strategy = PercentileEarlyStoppingStrategy(
            percentile_threshold=25,
        )
        should_stop = early_stopping_strategy.should_stop_trials_early(
            trial_indices=idcs, experiment=exp
        )
        self.assertEqual(should_stop, {0})

        early_stopping_strategy = PercentileEarlyStoppingStrategy(
            percentile_threshold=50,
        )
        should_stop = early_stopping_strategy.should_stop_trials_early(
            trial_indices=idcs, experiment=exp
        )
        self.assertEqual(should_stop, {0, 3})

        early_stopping_strategy = PercentileEarlyStoppingStrategy(
            percentile_threshold=75,
        )
        should_stop = early_stopping_strategy.should_stop_trials_early(
            trial_indices=idcs, experiment=exp
        )
        self.assertEqual(should_stop, {0, 3, 1})
