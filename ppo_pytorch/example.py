#!/usr/bin/env python3

import argparse
from functools import partial

import torch
from .ppo import PPO, create_atari_kwargs, create_fc_kwargs
from .common import GymWrapper, AtariVecEnv, SimpleVecEnv

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='PPO runner')
    parser.add_argument('--tensorboard-path', type=str, metavar='DIR', required=True,
                        help='tensorboard root output folder')
    parser.add_argument('--env-name', type=str, metavar='ENV', default='CartPole-v1',
                        help='gym env name')
    parser.add_argument('--steps', type=int, metavar='N',
                        help='number of executed environment steps across all actors; '
                             'one step is four frames for atari, one frame otherwise')
    parser.add_argument('--atari', action='store_true', default=False,
                        help='enable for atari envs')
    parser.add_argument('--no-cuda', action='store_true', default=False,
                        help='disable CUDA training for atari envs')
    parser.add_argument('--force-cuda', action='store_true', default=False,
                        help='enable CUDA training for non-atari envs')
    args = parser.parse_args()

    assert not args.atari or args.env_name.find('NoFrameskip') != -1, \
        'only NoFrameskip atari envs are supported, since library uses custom frameskip implementation.'
    assert not args.no_cuda or not args.force_cuda

    if args.force_cuda:
        args.cuda = torch.cuda.is_available()
    elif args.no_cuda:
        args.cuda = False
    else:
        # auto selection
        args.cuda = None

    # parameters for `PPO` class
    alg_params = create_atari_kwargs() if args.atari else create_fc_kwargs()

    if args.steps is not None:
        alg_params['learning_decay_frames'] = args.steps
    if args.cuda is not None:
        alg_params.update(dict(cuda_eval=args.cuda, cuda_train=args.cuda))

    rl_alg_factory = partial(PPO, **alg_params)
    env_factory = partial(AtariVecEnv, args.env_name) if args.atari else partial(SimpleVecEnv, args.env_name)
    gym_wrap = GymWrapper(
        rl_alg_factory,
        env_factory,
        log_time_interval=30 if args.atari else 5,
        log_path=args.tensorboard_path,
    )

    print('Training on {} for {} steps, CUDA {}'.format(
        args.env_name, int(alg_params['learning_decay_frames']),
        'enabled' if alg_params['cuda_train'] else 'disabled'))

    gym_wrap.train(alg_params['learning_decay_frames'])