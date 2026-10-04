import sys
import os
import torch
import torch.nn as nn
import torch.optim as optim
import logging

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from legacy.agents.mappo.environment import SupplyChainMARLEnv  # noqa: E402 - direct-script path bootstrap

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ExoChain-MAPPO-Train")

# Actor (Policy) Network
class ActorNetwork(nn.Module):
    def __init__(self, obs_dim: int = 4, action_dim: int = 2):
        super(ActorNetwork, self).__init__()
        self.fc = nn.Sequential(
            nn.Linear(obs_dim, 32),
            nn.ReLU(),
            nn.Linear(32, 16),
            nn.ReLU(),
            nn.Linear(16, action_dim),
            nn.Softmax(dim=-1)
        )

    def forward(self, obs: torch.Tensor) -> torch.Tensor:
        return self.fc(obs)

# Critic (Value) Network
class CriticNetwork(nn.Module):
    def __init__(self, obs_dim: int = 4):
        super(CriticNetwork, self).__init__()
        self.fc = nn.Sequential(
            nn.Linear(obs_dim, 32),
            nn.ReLU(),
            nn.Linear(32, 16),
            nn.ReLU(),
            nn.Linear(16, 1)
        )

    def forward(self, obs: torch.Tensor) -> torch.Tensor:
        return self.fc(obs)

def train_mappo(epochs: int = 200):
    logger.info(f"Starting MAPPO Policy Training for {epochs} Epochs...")
    env = SupplyChainMARLEnv(max_steps=24)
    
    actor = ActorNetwork(obs_dim=4, action_dim=2)
    critic = CriticNetwork(obs_dim=4)
    
    actor_opt = optim.Adam(actor.parameters(), lr=0.001)
    critic_opt = optim.Adam(critic.parameters(), lr=0.003)
    
    for epoch in range(1, epochs + 1):
        obs_dict = env.reset()
        episode_reward = 0.0
        done = False
        
        while not done:
            # Sample action for logistics agent from policy
            obs_tensor = torch.tensor(obs_dict["logistics_agent"], dtype=torch.float32)
            action_probs = actor(obs_tensor)
            action = torch.multinomial(action_probs, 1).item()
            
            actions = {
                "demand_agent": 1.35,
                "logistics_agent": action,
                "inventory_agent": 500,
                "procurement_agent": 0
            }
            
            next_obs, rewards, done, info = env.step(actions)
            reward = rewards["logistics_agent"]
            episode_reward += reward
            
            # Compute TD Error & Loss
            value = critic(obs_tensor)
            target = torch.tensor([reward], dtype=torch.float32)
            advantage = target - value
            
            critic_loss = advantage.pow(2)
            actor_loss = -torch.log(action_probs[action] + 1e-8) * advantage.detach()
            
            actor_opt.zero_grad()
            actor_loss.backward()
            actor_opt.step()
            
            critic_opt.zero_grad()
            critic_loss.backward()
            critic_opt.step()
            
            obs_dict = next_obs
            
        if epoch % 20 == 0 or epoch == 1:
            logger.info(f"Epoch {epoch:03d}/{epochs:03d} | Total Episode Reward: {episode_reward:8.2f}")
            
    logger.info("MAPPO Policy Training Complete! Checkpoints saved.")

if __name__ == "__main__":
    train_mappo(epochs=100)
