import math
from functools import partial
from typing import Optional, List, Callable

import gym
import gym.spaces
import numpy as np
import torch.nn as nn
import torch.nn.init as init
from torch import autograd
from torch.autograd import Variable
import torch
import torch.nn.functional as F

from .utils import weights_init, make_conv_heatmap, image_to_float
from ..common.make_grid import make_grid
from ..common.probability_distributions import make_pd, MultivecGaussianPd, FixedStdGaussianPd, \
    BernoulliPd, DiagGaussianPd, StaticTransactionPd, DiagGaussianTransactionPd
from .actors import Actor, CNNActor
from optfn.qrnn import QRNN, DenseQRNN
from optfn.sigmoid_pow import sigmoid_pow
from optfn.rnn.dlstm import DLSTM
from pretrainedmodels import nasnetamobile
from optfn.shuffle_conv import ShuffleConv2d
from optfn.swish import Swish
from .heads import ActorCriticHead
from optfn.temporal_group_norm import TemporalGroupNorm, TemporalLayerNorm


class QRNNActor(Actor):
    def __init__(self, observation_space: gym.Space, action_space: gym.Space, head_factory: Callable,
                 qrnn_hidden_size=128, qrnn_layers=3, **kwargs):
        """
        Args:
            observation_space: Env's observation space
            action_space: Env's action space
            head_factory: Function which accept (hidden vector size, `ProbabilityDistribution`) and return `HeadBase`
            hidden_sizes: List of hidden layers sizes
            activation: Activation function
        """
        super().__init__(observation_space, action_space, **kwargs)
        self.qrnn_hidden_size = qrnn_hidden_size
        self.qrnn_layers = qrnn_layers
        obs_len = int(np.product(observation_space.shape))
        self.qrnn = DenseQRNN(obs_len, qrnn_hidden_size, qrnn_layers, norm=self.norm)
        self.head = head_factory(qrnn_hidden_size, self.pd)
        self.reset_weights()

    def forward(self, input, memory, done_flags):
        x, next_memory = self.qrnn(input, memory, done_flags)
        head = self.head(x)
        return head, next_memory


class CNN_QRNNActor(CNNActor):
    def __init__(self, *args, qrnn_hidden_size=512, qrnn_layers=3, **kwargs):
        """
        Args:
            observation_space: Env's observation space
            action_space: Env's action space
            head_factory: Function which accept (hidden vector size, `ProbabilityDistribution`) and return `HeadBase`
            hidden_sizes: List of hidden layers sizes
            activation: Activation function
        """
        super().__init__(*args, **kwargs)
        del self.linear
        assert self.cnn_kind == 'large' # custom (2,066,432 parameters)
        nf = 32
        self.convs = nn.ModuleList([
            self.make_layer(nn.Conv2d(self.observation_space.shape[0], nf, 4, 2, 0, bias=False)),
            nn.MaxPool2d(3, 2),
            self.make_layer(nn.Conv2d(nf, nf * 2, 4, 2, 0, bias=False)),
            self.make_layer(nn.Conv2d(nf * 2, nf * 4, 4, 2, 1, bias=False)),
            self.make_layer(nn.Conv2d(nf * 4, nf * 8, 4, 2, 1, bias=False)),
        ])
        # self.linear = self.make_layer(nn.Linear(1024, 512))
        self.qrnn = DenseQRNN(512, qrnn_hidden_size, qrnn_layers)
        self.head = self.head_factory(qrnn_hidden_size, self.pd)
        self.reset_weights()

    def forward(self, input, memory, done_flags):
        seq_len, batch_len = input.shape[:2]
        input = input.contiguous().view(seq_len * batch_len, *input.shape[2:])

        input = image_to_float(input)
        x = self._extract_features(input)
        # x = x.view(seq_len * batch_len, -1)
        # x = self.linear(x)
        x = x.view(seq_len, batch_len, -1)
        x, next_memory = self.qrnn(x, memory, done_flags)

        head = self.head(x)

        if self.do_log:
            self.logger.add_histogram('conv linear', x, self._step)

        return head, next_memory


class Sega_CNN_QRNNActor(CNN_QRNNActor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        nf = 32
        in_c = self.observation_space.shape[0]
        self.convs = nn.ModuleList([
            self.make_layer(nn.Conv2d(in_c,   nf,     8, 4, 0, bias=self.norm is None)),
            self.make_layer(nn.Conv2d(nf,     nf * 2, 6, 3, 0, bias=self.norm is None)),
            self.make_layer(nn.Conv2d(nf * 2, nf * 4, 4, 2, 0, bias=self.norm is None)),
            # self.make_layer(nn.Conv2d(nf * 4, nf * 8, 3, 1, 0, bias=self.norm is None)),
        ])
        layer_norm = self.norm is not None and 'layer' in self.norm
        self.qrnn = DenseQRNN(1920, self.qrnn_hidden_size, self.qrnn_layers, layer_norm=layer_norm)
        self.reset_weights()


class Sega_CNN_HQRNNActor(Sega_CNN_QRNNActor):
    def __init__(self, *args, h_action_size=128, **kwargs):
        super().__init__(*args, **kwargs)
        self.h_action_size = h_action_size

        self.h_action_space = gym.spaces.Box(-1, 1, h_action_size)
        self.h_observation_space = gym.spaces.Box(-1, 1, self.qrnn_hidden_size)
        self.h_pd = make_pd(self.h_action_space)
        # self.gate_action_space = gym.spaces.Discrete(2)
        # self.gate_pd = make_pd(self.gate_action_space)

        layer_norm = self.norm is not None and 'layer' in self.norm

        # self.qrnn_l1 = self.qrnn
        del self.qrnn
        # self.qrnn_l2 = DenseQRNN(self.qrnn_hidden_size + h_action_size, self.qrnn_hidden_size, self.qrnn_layers, layer_norm=layer_norm)
        self.qrnn_l1 = nn.Sequential(
            nn.Linear(1920, self.qrnn_hidden_size),
            nn.ReLU(),
        )
        self.qrnn_l2 = nn.Sequential(
            nn.Linear(self.qrnn_hidden_size + h_action_size, self.qrnn_hidden_size),
            nn.ReLU(),
        )
        # self.action_upsample_l2 = nn.Sequential(
        #     nn.Linear(h_action_size, self.qrnn_hidden_size, bias=not layer_norm),
        #     *([TemporalLayerNorm1(self.qrnn_hidden_size)] if layer_norm else []),
        #     # nn.ReLU(),
        # )
        self.action_merge_l1 = nn.Sequential(
            nn.Linear(h_action_size * 2, self.qrnn_hidden_size, bias=not layer_norm),
            *([TemporalGroupNorm(1, self.qrnn_hidden_size)] if layer_norm else []),
            nn.ReLU(),
        )
        self.state_vec_extractor_l1 = nn.Sequential(
            nn.Linear(self.qrnn_hidden_size, h_action_size),
            TemporalGroupNorm(1, h_action_size, affine=False),
        )
        self.action_l2_norm = TemporalGroupNorm(1, h_action_size, affine=False)
        # self.norm_action_l2 = LayerNorm1d(self.qrnn_hidden_size, affine=False)
        # self.norm_hidden_l1 = LayerNorm1d(self.qrnn_hidden_size, affine=False)
        # self.head_gate_l2 = ActorCriticHead(self.qrnn_hidden_size, self.gate_pd)
        self.head_l2 = ActorCriticHead(self.qrnn_hidden_size, self.h_pd)
        self.reset_weights()

    def extract_l1_features(self, input):
        seq_len, batch_len = input.shape[:2]
        input = input.contiguous().view(seq_len * batch_len, *input.shape[2:])

        input = image_to_float(input)
        x = self._extract_features(input)
        x = x.view(seq_len, batch_len, -1)
        hidden_l1 = self.qrnn_l1(x)
        return hidden_l1

    def act_l1(self, hidden_l1, target_l1):
        x = torch.cat([hidden_l1, target_l1], -1)
        x = self.action_merge_l1(x)
        return x

    def forward(self, input, memory, done_flags, action_l2=None):
        # memory_l1, memory_l2 = memory.chunk(2, 0) if memory is not None else (None, None)

        hidden_l1 = self.extract_l1_features(input)
        state_vec_l1 = self.state_vec_extractor_l1(hidden_l1)
        input_l2 = torch.cat([hidden_l1, state_vec_l1], -1)
        # gate_l2 = self.head_gate_l2(hidden_l1)
        hidden_l2 = self.qrnn_l2(input_l2)

        head_l2 = self.head_l2(hidden_l2)
        if action_l2 is None:
            action_l2 = self.h_pd.sample(head_l2.probs)
        target_l2 = self.action_l2_norm(action_l2 + state_vec_l1).detach()

        preact_l1 = self.act_l1(state_vec_l1, target_l2)

        head_l1 = self.head(preact_l1)
        # head_l1.state_values = 0 * head_l1.state_values

        next_memory = Variable(input.new(2, input.shape[1], 2))

        return head_l1, head_l2, action_l2, state_vec_l1, target_l2, next_memory


class HQRNNActor(QRNNActor):
    def __init__(self, *args, qrnn_hidden_size=128, qrnn_layers=2, **kwargs):
        super().__init__(*args, qrnn_hidden_size=qrnn_hidden_size, qrnn_layers=qrnn_layers, **kwargs)
        self.h_action_size = self.qrnn_hidden_size

        self.h_pd = DiagGaussianTransactionPd(self.h_action_size)
        self.gate_pd = BernoulliPd(1)

        self.qrnn_l1 = self.qrnn
        del self.qrnn
        self.qrnn_l2 = DenseQRNN(self.qrnn_hidden_size, self.qrnn_hidden_size, self.qrnn_layers, norm=self.norm)

        # self.action_upsample_l2 = nn.Sequential(
        #     nn.Linear(h_action_size, self.qrnn_hidden_size, bias=not layer_norm),
        #     *([TemporalLayerNorm1(self.qrnn_hidden_size)] if layer_norm else []),
        #     # nn.ReLU(),
        # )
        self.action_merge_l1 = nn.Sequential(
            nn.Linear(self.h_action_size * 2, self.qrnn_hidden_size),
            # *([TemporalGroupNorm(1, self.qrnn_hidden_size)] if layer_norm else []),
            nn.ReLU(),
        )
        # self.state_vec_extractor_l1 = nn.Sequential(
        #     nn.Linear(self.qrnn_hidden_size, h_action_size),
        #     # TemporalGroupNorm(1, h_action_size, affine=False),
        # )
        # self.action_l2_norm = TemporalGroupNorm(1, h_action_size, affine=False)
        # self.norm_cur_l1 = TemporalLayerNorm(h_action_size, elementwise_affine=False)
        # self.norm_target_l1 = TemporalLayerNorm(h_action_size, elementwise_affine=False)
        # self.norm_hidden_l1 = LayerNorm1d(self.qrnn_hidden_size, affine=False)
        self.head_l2 = ActorCriticHead(self.qrnn_hidden_size, self.h_pd)
        self.head_gate_l2 = ActorCriticHead(self.qrnn_hidden_size, self.gate_pd, math.log(0.2))
        self.reset_weights()

    def forward(self, input, memory, done_flags, randn_l2=None):
        memory_l1, memory_l2 = memory.chunk(2, 0) if memory is not None else (None, None)

        hidden_l1, next_memory_l1 = self.qrnn_l1(input, memory_l1, done_flags)
        cur_l1 = hidden_l1 #= F.layer_norm(hidden_l1, hidden_l1.shape[-1:])
        # head_gate_l2 = self.head_gate_l2(hidden_l1)
        # if action_gate_l2 is None:
        #     # (actors, batch, 1)
        #     action_gate_l2 = self.gate_pd.sample(head_gate_l2.probs)
        hidden_l2, next_memory_l2 = self.qrnn_l2(hidden_l1, memory_l2, done_flags)
        # hidden_l2 = F.layer_norm(hidden_l2, hidden_l2.shape[-1:])
        # cur_l1 = self.state_vec_extractor_l1(hidden_l1)
        # cur_l1 = hidden_l1

        head_l2 = self.head_l2(hidden_l2)
        action_l2, randn_l2 = self.h_pd.sample(head_l2.probs, randn_l2)
        target_l1 = (cur_l1 + action_l2).detach()
        cur_l1_norm = F.layer_norm(cur_l1, cur_l1.shape[-1:])
        target_l1_norm = F.layer_norm(target_l1, target_l1.shape[-1:])

        preact_l1 = torch.cat([cur_l1_norm, target_l1_norm], -1)
        preact_l1 = self.action_merge_l1(preact_l1)
        head_l1 = self.head(preact_l1)

        next_memory = torch.cat([next_memory_l1, next_memory_l2], 0)
        # head_l1.state_values = head_l1.state_values * 0

        return head_l1, head_l2, action_l2, randn_l2, cur_l1, target_l1, next_memory
