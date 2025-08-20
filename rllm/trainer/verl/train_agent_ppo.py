# Copyright under Agentica Project.
"""
Note that we don't combine the main with ray_trainer as ray_trainer is used by other main.
"""

import hydra
import ray
from verl.trainer.ppo.reward import load_reward_manager

from rllm.trainer.env_agent_mappings import AGENT_CLASS_MAPPING, ENV_CLASS_MAPPING, WORKFLOW_CLASS_MAPPING
from rllm.trainer.verl.agent_ppo_trainer import AgentPPOTrainer

# Local application imports
from rllm.trainer.verl.agent_workflow_trainer import AgentWorkflowPPOTrainer


@hydra.main(config_path="../config", config_name="agent_ppo_trainer", version_base=None)
def main(config):
    run_ppo_agent(config)


def run_ppo_agent(config):
    if not ray.is_initialized():
        # this is for local ray cluster
        ray.init(runtime_env={"env_vars": {"TOKENIZERS_PARALLELISM": "true", "NCCL_DEBUG": "WARN"}})

    ray.get(train_agent.remote(config))


@ray.remote(num_cpus=1)  # please make sure main_task is not scheduled on head
def train_agent(config, workflow_class=None, workflow_args=None, agent_class=None, env_class=None, agent_args=None, env_args=None):
    # print initial config
    from pprint import pprint

    from omegaconf import OmegaConf
    from verl.utils.fs import copy_local_path_from_hdfs

    OmegaConf.register_new_resolver("mul", lambda x, y: int(x) * int(y))
    OmegaConf.resolve(config)
    pprint(OmegaConf.to_container(config))

    # download the checkpoint from hdfs
    local_path = copy_local_path_from_hdfs(config.actor_rollout_ref.model.path)

    # instantiate tokenizer
    from verl.utils import hf_tokenizer

    trust_remote_code = config.data.get("trust_remote_code", False)
    tokenizer = hf_tokenizer(local_path, trust_remote_code=trust_remote_code)
    # processor = hf_processor(local_path, use_fast=True)  # used for multimodal LLM, could be none

    if config.actor_rollout_ref.actor.strategy in ["fsdp", "fsdp2"]:
        assert config.critic.strategy in ["fsdp", "fsdp2"]
        from verl.single_controller.ray import RayWorkerGroup
        from verl.workers.fsdp_workers import ActorRolloutRefWorker, AsyncActorRolloutRefWorker, CriticWorker

        actor_rollout_cls = AsyncActorRolloutRefWorker if config.actor_rollout_ref.rollout.mode == "async" else ActorRolloutRefWorker
        ray_worker_group_cls = RayWorkerGroup
    else:
        raise NotImplementedError

    from verl.trainer.ppo.ray_trainer import ResourcePoolManager, Role

    role_worker_mapping = {
        Role.ActorRollout: ray.remote(max_concurrency=2048)(actor_rollout_cls),
        Role.Critic: ray.remote(CriticWorker),
    }

    global_pool_id = "global_pool"
    resource_pool_spec = {
        global_pool_id: [config.trainer.n_gpus_per_node] * config.trainer.nnodes,
    }
    mapping = {
        Role.ActorRollout: global_pool_id,
        Role.Critic: global_pool_id,
    }

    if config.algorithm.use_kl_in_reward or config.actor_rollout_ref.actor.use_kl_loss:
        role_worker_mapping[Role.RefPolicy] = ray.remote(ActorRolloutRefWorker)
        mapping[Role.RefPolicy] = global_pool_id

    reward_fn = load_reward_manager(config, tokenizer, num_examine=0, **config.reward_model.get("reward_kwargs", {}))
    val_reward_fn = load_reward_manager(config, tokenizer, num_examine=1)
    resource_pool_manager = ResourcePoolManager(resource_pool_spec=resource_pool_spec, mapping=mapping)

    if config.rllm.workflow.use_workflow:
        if workflow_class is None:
            workflow_class = WORKFLOW_CLASS_MAPPING[config.rllm.workflow.name]
        workflow_args = workflow_args or {}
        if config.rllm.workflow.get("workflow_args") is not None:
            workflow_args.update(config.rllm.workflow.get("workflow_args"))

        trainer = AgentWorkflowPPOTrainer(
            config=config,
            tokenizer=tokenizer,
            role_worker_mapping=role_worker_mapping,
            resource_pool_manager=resource_pool_manager,
            ray_worker_group_cls=ray_worker_group_cls,
            reward_fn=reward_fn,
            val_reward_fn=val_reward_fn,
            workflow_class=workflow_class,
            workflow_args=workflow_args,
        )

    else:
        if env_class is None:
            env_class = ENV_CLASS_MAPPING[config.rllm.env.name]
        if agent_class is None:
            agent_class = AGENT_CLASS_MAPPING[config.rllm.agent.name]

        env_args = env_args or {}
        agent_args = agent_args or {}
        if config.rllm.env.get("env_args") is not None:
            env_args.update(config.rllm.env.get("env_args"))
        if config.rllm.agent.get("agent_args") is not None:
            agent_args.update(config.rllm.agent.get("agent_args"))

        trainer = AgentPPOTrainer(
            config=config,
            tokenizer=tokenizer,
            role_worker_mapping=role_worker_mapping,
            resource_pool_manager=resource_pool_manager,
            ray_worker_group_cls=ray_worker_group_cls,
            reward_fn=reward_fn,
            val_reward_fn=val_reward_fn,
            env_class=env_class,
            agent_class=agent_class,
            env_args=env_args,
            agent_args=agent_args,
        )

    trainer.init_workers()
    try:
        trainer.fit_agent()
    finally:
        trainer.shutdown()


if __name__ == "__main__":
    main()
