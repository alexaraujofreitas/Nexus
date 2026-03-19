"""RL Agents package — SAC, CPPO, Duelling DQN."""

try:
    from core.rl.agents.sac_agent import SACAgent
    from core.rl.agents.cppo_agent import CPPOAgent
    from core.rl.agents.duelling_dqn_agent import DuellingDQNAgent
    __all__ = ["SACAgent", "CPPOAgent", "DuellingDQNAgent"]
except ImportError as e:
    # PyTorch or other dependencies not available
    __all__ = []
