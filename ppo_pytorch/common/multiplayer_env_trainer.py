import pprint
import random
from itertools import count
from typing import Callable, List

import numpy as np
import gym

from .rl_base import RLBase
from .env_factory import NamedVecEnv
from .tensorboard_env_logger import TensorboardEnvLogger


class MultiplayerEnvTrainer:
    def __init__(self,
                 rl_alg_factory: Callable,
                 env_factory: Callable[[], NamedVecEnv],
                 population_size: int,
                 selection_train_frames: int,
                 log_path,
                 log_interval=10 * 1024,
                 tag=''):
        """
        Simplifies training of RL algorithms with gym environments.
        Args:
            rl_alg_factory: RL algorithm factory / type.
            env: Environment factory.
                Accepted values are environment name, function which returns `gym.Env`, `gym.Env` object
            log_interval: Tensorboard logging interval in frames.
            log_path: Tensorboard output directory.
        """
        self._init_args = locals()
        self.rl_alg_factory = rl_alg_factory
        self.env_factory = env_factory
        self.population_size = population_size
        self.selection_train_frames = selection_train_frames
        self.log_path = log_path
        self.log_interval = log_interval
        self.tag = tag

        self.env: NamedVecEnv = env_factory()
        self.frame: int = 0
        self.selected_algs: List[RLBase] = None
        self.selected_loggers: List[TensorboardEnvLogger] = None
        self.selection_change_frame: int = None
        # self.all_rewards: List[List[float]] = []

        self.rl_algs: List[RLBase] = []
        self.loggers: List[TensorboardEnvLogger] = []
        for pop_i in range(population_size):
            alg = rl_alg_factory(self.env.observation_space, self.env.action_space,
                                 log_interval=log_interval, actor_index=pop_i)
            env_name = self.env.env_name
            alg_name = type(alg).__name__
            if pop_i == 0:
                self.env.set_num_envs(alg.num_actors)
            logger = TensorboardEnvLogger(alg_name, env_name, log_path, self.env.num_envs,
                                          log_interval, tag=f'{tag}_{pop_i}')
            logger.add_text('MultiplayerEnvTrainer', pprint.pformat(self._init_args))
            alg.logger = logger
            self.rl_algs.append(alg)
            self.loggers.append(logger)

        self.all_states: np.ndarray = self.env.reset().transpose(1, 0, 2)
        self.num_players = self.all_states.shape[0]
        assert self.population_size >= self.num_players

    def step(self, always_log=False):
        """Do single step of RL alg"""

        self._try_update_selection()

        # evaluate RL alg
        actions = [alg.eval(st) for (alg, st) in zip(self.selected_algs, self.all_states)]
        actions = np.array(actions).T
        data = self.env.step(actions)
        self.all_states, all_rewards, dones, all_infos = [np.asarray(v) for v in data]
        self.all_states, all_rewards, all_infos = self.all_states.transpose(1, 0, 2), all_rewards.T, all_infos.T

        assert self.all_states.shape[0] == all_rewards.shape[0]

        # # process step results
        # for info in all_infos:
        #     ep_info = info.get('episode')
        #     if ep_info is not None:
        #         self.all_rewards.append(ep_info)

        # send all_rewards and done flags to rl alg
        for alg, rewards, infos, logger in zip(self.selected_algs, all_rewards, all_infos, self.selected_loggers):
            alg.reward(rewards)
            alg.finish_episodes(dones)
            logger.step(infos, always_log)

        self.frame += self.env.num_envs

    def train(self, max_frames):
        """Train for specified number of frames and return episode info"""
        # self.all_rewards = []
        for _ in count():
            stop = self.frame + self.env.num_envs >= max_frames
            self.step(stop)
            if stop:
                break
        # return self.all_rewards

    def _try_update_selection(self):
        if self.selected_algs is not None \
                and self.selection_change_frame + self.selection_train_frames > self.frame:
            return
        self.selection_change_frame = self.frame

        print('change', self.frame)

        if self.selected_algs is not None:
            for alg in self.selected_algs:
                alg.drop_collected_steps()

        indexes = np.random.choice(np.arange(self.population_size), self.num_players, replace=False)
        self.selected_algs = np.take(self.rl_algs, indexes)
        self.selected_loggers = np.take(self.loggers, indexes)