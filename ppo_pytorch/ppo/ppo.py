import pprint
from collections import namedtuple
from functools import partial

import gym.spaces
import numpy as np
import torch
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable
from torch.nn.utils import clip_grad_norm
from torch.utils.data import DataLoader
from torchvision.utils import make_grid

from ..common import DecayLR, ValueDecay
from ..common.gae import calc_advantages, calc_returns
from ..common.multi_dataset import MultiDataset
from ..common.probability_distributions import DiagGaussianPd
from ..common.rl_base import RLBase
from ..models import MLPActorCritic
from ..common.param_groups_getter import get_param_groups
from pathlib import Path
import math


def lerp(start, end, weight):
    """
    Same as torch.lerp, but tensors allowed at `weight`
    """
    return start + (end - start) * weight


def clamp(x, min, max):
    """
    Same as torch.clamp, but tensors allowed at `min` and `max`
    """
    x = torch.max(x, min) if isinstance(min, Variable) else x.clamp(min=min)
    x = torch.min(x, max) if isinstance(max, Variable) else x.clamp(max=max)
    return x


# used to store env step data for training
Sample = namedtuple('Sample', 'states, rewards, dones, probs, values, actions')
# preprocessed steps for use in in PPO training loop
TrainingData = namedtuple('TrainingData', 'states, probs_old, values_old, actions, advantages, returns, dones, rewards')


class PPO(RLBase):
    """Single threaded implementation of Proximal Policy Optimization Algorithms

    https://arxiv.org/pdf/1707.06347.pdf
    """

    def __init__(self, observation_space, action_space,
                 reward_discount=0.99,
                 advantage_discount=0.95,
                 horizon=64,
                 ppo_iters=10,
                 batch_size=64,
                 model_factory=MLPActorCritic,
                 optimizer_factory=partial(optim.Adam, lr=3e-4),
                 value_loss_scale=0.5,
                 entropy_bonus=0.01,
                 constraint='clip',
                 policy_clip=0.1,
                 value_clip=0.1,
                 cuda_eval=False,
                 cuda_train=False,
                 grad_clip_norm=2,
                 reward_scale=1.0,
                 image_observation=True,
                 lr_scheduler_factory=None,
                 clip_decay_factory=None,
                 entropy_decay_factory=None,
                 model_save_folder=None,
                 model_save_interval=None,
                 model_init_path=None,
                 save_intermediate_models=False,
                 model_save_tag='ppo_model',
                 **kwargs):
        """
        Args:
            observation_space (gym.Space): Environment's observation space
            action_space (gym.Space): Environment's action space
            reward_discount (float): Value function discount factor
            advantage_discount (float): Global Advantage Estimation discount factor
            num_actors (int): Number of parallel environments (evaluation batch size)
            horizon (int): Training will happen each `horizon` * `num_actors` steps
            ppo_iters (int): Number training passes for each state
            batch_size (int): Training batch size
            model_factory (Callable[[gym.Space, gym.Space], nn.Model]):
                Callable object receiving (observation_space, action_space) and returning actor-critic model
            optimizer_factory (Callable[[List[nn.Parameter]], optim.Optimizer]):
                Callable object receiving `model.parameters()` and returning model optimizer
            value_loss_scale (float): Multiplier for state-value loss
            entropy_bonus (float): Entropy bonus
            constraint (str):
                None - No constraint
                'clip' - PPO clipping
                'clip_mod' - Modified PPO clipping. May be better for large clip factors.
            policy_clip (float): policy clip strength
            value_clip (float): State-value clip strength
            cuda_eval (bool): Use CUDA for environment steps
            cuda_train (bool): Use CUDA for training steps
            grad_clip_norm (float): Max norm for gradient clipping
            log_time_interval (float): Tensorboard logging interval in seconds.
            learning_decay_frames (int): Learning rate, clip strength, entropy bonus
                will decrease to 0 after `learning_decay_frames`
            reward_scale (float): Scale factor for environment's rewards
        """
        super().__init__(observation_space, action_space, **kwargs)
        self._init_args = locals()
        self.reward_discount = reward_discount
        self.advantage_discount = advantage_discount
        self.policy_clip = policy_clip
        self.value_clip = value_clip
        self.entropy_bonus = entropy_bonus
        self.horizon = horizon
        self.ppo_iters = ppo_iters
        self.batch_size = batch_size
        self.cuda_eval = cuda_eval
        self.cuda_train = cuda_train
        self.grad_clip_norm = grad_clip_norm
        self.value_loss_scale = value_loss_scale
        self.model_factory = model_factory
        self.constraint = constraint
        self.reward_scale = reward_scale
        self.image_observation = image_observation
        self.model_save_folder = model_save_folder
        self.model_save_interval = model_save_interval
        self.save_intermediate_models = save_intermediate_models
        self.model_save_tag = model_save_tag

        assert constraint in (None, 'clip', 'clip_mod')
        assert not image_observation or \
               isinstance(observation_space, gym.spaces.Box) and len(observation_space.shape) == 3

        self.sample = self.create_new_sample()

        self.model = model_factory(observation_space, action_space)
        if model_init_path is not None:
            self.model.load_state_dict(torch.load(model_init_path))
        self.optimizer = optimizer_factory(get_param_groups(self.model))
        self.lr_scheduler = lr_scheduler_factory(self.optimizer) if lr_scheduler_factory is not None else None
        self.clip_decay = clip_decay_factory() if clip_decay_factory is not None else None
        self.entropy_decay = entropy_decay_factory() if entropy_decay_factory is not None else None
        self.last_model_save_frame = 0

    @property
    def learning_rate(self):
        return self.optimizer.param_groups[0]['lr']

    @property
    def clip_mult(self):
        return self.clip_decay.value if self.clip_decay is not None else 1

    def _step(self, prev_states, rewards, dones, cur_states) -> np.ndarray:
        # move network to cuda or cpu
        if next(self.model.parameters()).is_cuda != self.cuda_eval:
            self.model = self.model.cuda() if self.cuda_eval else self.model.cpu()

        # self.model.eval()

        # convert observations to tensors
        if self.image_observation:
            states = self._from_numpy(cur_states * 255, dtype=np.uint8, cuda=False)
        else:
            states = self._from_numpy(cur_states, dtype=np.float32, cuda=False)

        # run network
        # ac_out = self.model(Variable(states, volatile=True))
        ac_out = self._take_step(states.cuda() if self.cuda_eval else states, dones)
        actions = self.model.pd.sample(ac_out.probs).data.cpu().numpy()
        probs, values = ac_out.probs.data.cpu().numpy(), ac_out.state_values.data.cpu().numpy()

        self.append_to_sample(self.sample, states, rewards, dones, actions, probs, values)

        if len(self.sample.rewards) >= self.horizon:
            self._train()

        return actions

    def _take_step(self, states, dones):
        return self.model(Variable(states, volatile=True))

    def _train(self):
        self._check_log()

        # update clipping and learning rate decay schedulers
        if self.lr_scheduler is not None:
            self.lr_scheduler.step(self.frame)
        if self.clip_decay is not None:
            self.clip_decay.step(self.frame)
        if self.entropy_decay is not None:
            self.entropy_decay.step(self.frame)

        data = self._process_sample(self.sample)
        self._log_training_data(data)
        self._ppo_update(data)
        self.check_save_model()
        self.sample = self.create_new_sample()

    def _log_training_data(self, data):
        if self._do_log:
            if data.states.dim() == 4:
                if data.states.shape[1] in (1, 3):
                    img = data.states[:4]
                    nrow = 2
                else:
                    img = data.states[:4]
                    img = img.view(-1, *img.shape[2:]).unsqueeze(1)
                    nrow = data.states.shape[1]
                if self.image_observation:
                    img = img.float() / 255
                img = make_grid(img, nrow=nrow, normalize=False)
                self.logger.add_image('state', img, self.frame)
            self.logger.add_histogram('rewards', data.rewards, self.frame)
            self.logger.add_histogram('returns', data.returns, self.frame)
            self.logger.add_histogram('advantages', data.advantages, self.frame)
            self.logger.add_histogram('values', data.values_old, self.frame)
            if isinstance(self.model.pd, DiagGaussianPd):
                mean, std = data.probs_old.chunk(2, dim=1)
                self.logger.add_histogram('probs mean', mean, self.frame)
                self.logger.add_histogram('probs std', std, self.frame)
            else:
                self.logger.add_histogram('probs', F.log_softmax(Variable(data.probs_old), dim=-1), self.frame)
            for name, param in self.model.named_parameters():
                self.logger.add_histogram(name, param, self.frame)

    @staticmethod
    def append_to_sample(sample, states, rewards, dones, actions, probs, values):
        # add step to history
        if len(sample.states) != 0:
            sample.rewards.append(rewards)
            sample.dones.append(dones)
        sample.states.append(states.cpu())
        sample.probs.append(probs)
        sample.values.append(values)
        sample.actions.append(actions)

    @staticmethod
    def create_new_sample():
        return Sample([], [], [], [], [], [])

    def _process_sample(self, sample, pd=None, reward_discount=None, advantage_discount=None, reward_scale=None):
        if pd is None:
            pd = self.model.pd
        if reward_discount is None:
            reward_discount = self.reward_discount
        if advantage_discount is None:
            advantage_discount = self.advantage_discount
        if reward_scale is None:
            reward_scale = self.reward_scale

        # convert list to numpy array
        # (seq, num_actors, ...)
        rewards = np.asarray(sample.rewards)
        values_old = np.asarray(sample.values)
        dones = np.asarray(sample.dones)

        norm_rewards, np_returns, np_advantages = self._process_rewards(
            rewards, values_old, dones, reward_discount, advantage_discount, reward_scale)

        # (seq * num_actors, ...)
        np_advantages = np_advantages.flatten()
        np_returns = np_returns.flatten()
        dones = dones.flatten()
        norm_rewards = norm_rewards.flatten()

        # convert data to Tensors
        probs_old = torch.cat(self._from_numpy(sample.probs[:-1], dtype=np.float32), dim=0)
        values_old = torch.cat(self._from_numpy(sample.values[:-1], dtype=np.float32), dim=0)
        actions = torch.cat(self._from_numpy(sample.actions[:-1], dtype=pd.dtype_numpy), dim=0)
        returns = self._from_numpy(np_returns, np.float32)
        advantages = self._from_numpy(np_advantages, np.float32)
        states = torch.cat(sample.states[:-1], dim=0) if sample.states is not None else None
        dones = self._from_numpy(dones, np.float32)
        rewards = self._from_numpy(norm_rewards, np.float32)

        return TrainingData(states, probs_old, values_old, actions, advantages, returns, dones, rewards)

    def _process_rewards(self, rewards, values, dones, reward_discount, advantage_discount, reward_scale):
        norm_rewards = reward_scale * rewards

        # calculate returns and advantages
        np_returns = calc_returns(norm_rewards, values, dones, reward_discount)
        np_advantages = calc_advantages(norm_rewards, values, dones, reward_discount, advantage_discount)
        np_advantages = (np_advantages - np_advantages.mean()) / max(np_advantages.std(), 1e-5)
        # np_advantages = np_advantages / np.sqrt((np_advantages ** 2).mean())

        return norm_rewards, np_returns, np_advantages

    def _ppo_update(self, data):
        self.model.train()
        # move model to cuda or cpu
        if next(self.model.parameters()).is_cuda != self.cuda_train:
            self.model = self.model.cuda() if self.cuda_train else self.model.cpu()

        data = (data.states.pin_memory() if self.cuda_train else data.states,
                data.probs_old, data.values_old, data.actions, data.advantages, data.returns)
        batches = max(1, self.num_actors * self.horizon // self.batch_size)

        for ppo_iter in range(self.ppo_iters):
            rand_idx = torch.randperm(len(data[0]))
            for loader_iter in range(batches):
                # prepare batch data
                batch_idx = rand_idx[loader_iter * self.batch_size: (loader_iter + 1) * self.batch_size]
                batch_idx_cuda = batch_idx.cuda()
                st, po, vo, ac, adv, ret = [Variable(x[batch_idx_cuda if x.is_cuda else batch_idx]) for x in data]
                if self.cuda_train:
                    st = Variable(st.data.cuda())
                if ppo_iter == self.ppo_iters - 1 and loader_iter == 0:
                    self.model.set_log(self.logger, self._do_log, self.step)
                actor_out = self.model(st)
                # get loss
                loss, kl = self._get_ppo_loss(actor_out.probs.cpu(), po, actor_out.state_values.cpu(), vo, ac, adv, ret)

                # optimize
                loss.backward()
                clip_grad_norm(self.model.parameters(), self.grad_clip_norm)
                self.optimizer.step()
                self.optimizer.zero_grad()

                self.model.set_log(self.logger, False, self.step)

            if self._do_log and ppo_iter == self.ppo_iters - 1:
                self.logger.add_scalar('learning rate', self.learning_rate, self.frame)
                self.logger.add_scalar('clip mult', self.clip_mult, self.frame)
                self.logger.add_scalar('total loss', loss, self.frame)
                self.logger.add_scalar('kl', kl, self.frame)

    def _from_numpy(self, x, dtype=None, cuda=False):
        """
        Helper function which converts input to Tensor
        Args:
            x: Anything from which numpy array could be created
            dtype: Numpy input type. `x` is cast to it.
            cuda: Transfer tensor to CUDA

        Returns: Tensor created from `x`
        """
        x = np.asarray(x, dtype=dtype)
        x = torch.from_numpy(x)
        if cuda:
            x = x.cuda()
        return x

    def _get_ppo_loss(self, probs, probs_old, values, values_old, actions, advantages, returns, pd=None, tag=''):
        """
        Single iteration of PPO algorithm.
        Returns: Total loss and KL divergence.
        """

        if pd is None:
            pd = self.model.pd

        # prepare data
        actions = actions.detach()
        values, values_old, actions, advantages, returns = \
            [x.squeeze() for x in (values, values_old, actions, advantages, returns)]

        # clipping factors
        value_clip = self.value_clip * self.clip_mult
        policy_clip = self.policy_clip * self.clip_mult

        # action probability ratio
        # log probabilities used for better numerical stability
        logp = pd.logp(actions, probs)
        logp_old = pd.logp(actions, probs_old).detach()
        ratio = logp - logp_old #(logp - logp_old).exp()

        # policy loss
        unclipped_policy_loss = ratio * advantages
        if self.constraint == 'clip' or self.constraint == 'clip_mod':
            # clip policy loss
            clipped_ratio = ratio.clamp(-policy_clip, policy_clip)
            clipped_policy_loss = clipped_ratio * advantages
            loss_clip = -torch.min(unclipped_policy_loss, clipped_policy_loss)
        else:
            # do not clip loss
            loss_clip = -unclipped_policy_loss

        # value loss
        v_pred_clipped = values_old + (values - values_old).clamp(-value_clip, value_clip)
        vf_clip_loss = F.smooth_l1_loss(v_pred_clipped, returns, reduce=False)
        vf_nonclip_loss = F.smooth_l1_loss(values, returns, reduce=False)
        loss_value = self.value_loss_scale * 0.5 * torch.max(vf_nonclip_loss, vf_clip_loss)

        # entropy bonus for better exploration
        entropy = pd.entropy(probs)
        loss_ent = -self.entropy_bonus * entropy

        # sum all losses
        total_loss = loss_clip.mean() + loss_value.mean() + loss_ent.mean()
        assert not np.isnan(total_loss.data[0]) and not np.isinf(total_loss.data[0]), \
            (loss_clip.data.mean(), loss_value.data.mean(), loss_ent.data.mean())

        kl = pd.kl(probs_old, probs).mean()

        if self.model.do_log:
            self.logger.add_histogram('loss value' + tag, loss_value, self.frame)
            self.logger.add_histogram('loss ent' + tag, loss_ent, self.frame)
            self.logger.add_scalar('entropy' + tag, entropy.mean(), self.frame)
            self.logger.add_scalar('loss entropy' + tag, loss_ent.mean(), self.frame)
            self.logger.add_scalar('loss value' + tag, loss_value.mean(), self.frame)
            self.logger.add_histogram('ratio' + tag, ratio, self.frame)
            self.logger.add_scalar('ratio mean' + tag, ratio.mean(), self.frame)
            self.logger.add_scalar('ratio abs mean' + tag, (ratio - 1).abs().mean(), self.frame)
            self.logger.add_scalar('ratio abs max' + tag, (ratio - 1).abs().max(), self.frame)
            if self.constraint == 'clip' or self.constraint == 'clip_mod':
                self.logger.add_histogram('loss clip' + tag, loss_clip, self.frame)
                self.logger.add_scalar('loss clip' + tag, loss_clip.mean(), self.frame)

        return total_loss, kl

    def _log_set(self):
        self.logger.add_text('PPO', pprint.pformat(self._init_args))

    def drop_collected_steps(self):
        self.sample = Sample(states=[], probs=[], values=[], actions=[], rewards=[], dones=[])

    def check_save_model(self):
        if self.model_save_interval is None or \
           self.last_model_save_frame + self.model_save_interval > self.frame:
            return
        self.last_model_save_frame = self.frame
        name = f'{self.model_save_tag}_{self.frame}' if self.save_intermediate_models else self.model_save_tag
        path = Path(self.model_save_folder) / (name + '.pth')
        print('saving to path', path)
        torch.save(self.model.state_dict(), path)
