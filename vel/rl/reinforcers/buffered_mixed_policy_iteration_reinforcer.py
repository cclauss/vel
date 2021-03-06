import attr
import numpy as np
import sys
import torch
import tqdm

from vel.api import EpochInfo, BatchInfo
from vel.api.base import Model, ModelFactory
from vel.openai.baselines.common.vec_env import VecEnv
from vel.rl.api.base import ReinforcerBase, ReinforcerFactory, VecEnvFactory, ReplayEnvRollerBase, AlgoBase
from vel.rl.api.base.env_roller import ReplayEnvRollerFactory
from vel.rl.metrics import (
    FPSMetric, EpisodeLengthMetric, EpisodeRewardMetricQuantile,
    EpisodeRewardMetric, FramesMetric
)


@attr.s(auto_attribs=True)
class BufferedMixedPolicyIterationReinforcerSettings:
    """ Settings dataclass for a policy gradient reinforcer """
    number_of_steps: int
    discount_factor: float
    experience_replay: int = 1
    stochastic_experience_replay: bool = True


class BufferedMixedPolicyIterationReinforcer(ReinforcerBase):
    """ Factory class replay buffer reinforcer """

    def __init__(self, device: torch.device, settings: BufferedMixedPolicyIterationReinforcerSettings, env: VecEnv,
                 model: Model, env_roller: ReplayEnvRollerBase, algo: AlgoBase) -> None:
        self.device = device
        self.settings = settings

        self.environment = env
        self._trained_model = model.to(self.device)

        self.env_roller = env_roller
        self.algo = algo

    def metrics(self) -> list:
        """ List of metrics to track for this learning process """
        my_metrics = [
            FramesMetric("frames"),
            FPSMetric("fps"),
            EpisodeRewardMetric('PMM:episode_rewards'),
            EpisodeRewardMetricQuantile('P09:episode_rewards', quantile=0.9),
            EpisodeRewardMetricQuantile('P01:episode_rewards', quantile=0.1),
            EpisodeLengthMetric("episode_length")
        ]

        return my_metrics + self.algo.metrics() + self.env_roller.metrics()

    @property
    def model(self) -> Model:
        """ Model trained by this reinforcer """
        return self._trained_model

    def initialize_training(self, training_info):
        """ Prepare models for training """
        self.model.reset_weights()
        self.algo.initialize(self.settings, model=self.model, environment=self.environment, device=self.device)

    def train_epoch(self, epoch_info: EpochInfo):
        """ Train model on an epoch of a fixed number of batch updates """
        epoch_info.on_epoch_begin()

        for batch_idx in tqdm.trange(epoch_info.batches_per_epoch, file=sys.stdout, desc="Training", unit="batch"):
            batch_info = BatchInfo(epoch_info, batch_idx)

            batch_info.on_batch_begin()
            self.train_batch(batch_info)
            batch_info.on_batch_end()

        epoch_info.result_accumulator.freeze_results()
        epoch_info.on_epoch_end()

    def train_batch(self, batch_info: BatchInfo):
        """ Single, most atomic 'step' of learning this reinforcer can perform """
        batch_info['sub_batch_data'] = []

        self.on_policy_train_batch(batch_info)

        if self.settings.experience_replay > 0 and self.env_roller.is_ready_for_sampling():
            if self.settings.stochastic_experience_replay:
                experience_replay_count = np.random.poisson(self.settings.experience_replay)
            else:
                experience_replay_count = self.settings.experience_replay

            for i in range(experience_replay_count):
                self.off_policy_train_batch(batch_info)

        # Even with all the experience replay, we count the single rollout as a single batch
        batch_info.aggregate_key('sub_batch_data')

    def on_policy_train_batch(self, batch_info: BatchInfo):
        """ Perform an 'on-policy' training step of evaluating an env and a single backpropagation step """
        self.model.eval()
        rollout = self.env_roller.rollout(batch_info, self.model)

        self.model.train()
        batch_result = self.algo.optimizer_step(
            batch_info=batch_info,
            device=self.device,
            model=self.model,
            rollout=rollout
        )

        batch_info['sub_batch_data'].append(batch_result)
        batch_info['frames'] = rollout['size']
        batch_info['episode_infos'] = rollout['episode_information']

    def off_policy_train_batch(self, batch_info: BatchInfo):
        """ Perform an 'off-policy' training step of sampling the replay buffer and gradient descent """
        self.model.eval()
        rollout = self.env_roller.sample(batch_info, self.model)

        self.model.train()
        batch_result = self.algo.optimizer_step(
            batch_info=batch_info,
            device=self.device,
            model=self.model,
            rollout=rollout
        )

        batch_info['sub_batch_data'].append(batch_result)


class BufferedMixedPolicyIterationReinforcerFactory(ReinforcerFactory):
    """ Factory class for the PolicyGradientReplayBuffer factory """
    def __init__(self, settings, env_factory: VecEnvFactory, model_factory: ModelFactory,
                 env_roller_factory: ReplayEnvRollerFactory, algo: AlgoBase, parallel_envs: int, seed: int):
        self.settings = settings

        self.model_factory = model_factory
        self.env_factory = env_factory
        self.parallel_envs = parallel_envs
        self.env_roller_factory = env_roller_factory
        self.algo = algo
        self.seed = seed

    def instantiate(self, device: torch.device) -> ReinforcerBase:
        env = self.env_factory.instantiate(parallel_envs=self.parallel_envs, seed=self.seed)
        model = self.model_factory.instantiate(action_space=env.action_space)
        env_roller = self.env_roller_factory.instantiate(env, device, self.settings)

        return BufferedMixedPolicyIterationReinforcer(device, self.settings, env, model, env_roller, self.algo)


def create(model_config, model, vec_env, algo, env_roller, number_of_steps,
           parallel_envs, discount_factor,
           experience_replay=1, stochastic_experience_replay=True):
    """ Create a policy gradient reinforcer - factory """
    settings = BufferedMixedPolicyIterationReinforcerSettings(
        number_of_steps=number_of_steps,
        discount_factor=discount_factor,
        experience_replay=experience_replay,
        stochastic_experience_replay=stochastic_experience_replay
    )

    return BufferedMixedPolicyIterationReinforcerFactory(
        settings,
        env_factory=vec_env,
        model_factory=model,
        parallel_envs=parallel_envs,
        env_roller_factory=env_roller,
        algo=algo,
        seed=model_config.seed
    )
