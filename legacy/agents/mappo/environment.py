import sys
import os
import numpy as np
import logging

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ExoChain-MAPPO")

class SupplyChainMARLEnv:
    """
    Multi-Agent Supply Chain Environment for MAPPO optimization.
    
    Agents:
      1. Demand Agent (Action: Safety Stock Multiplier [1.0x - 2.0x])
      2. Logistics Agent (Action: Route Shift [0: Maritime, 1: Air Express])
      3. Inventory Agent (Action: Order Quantity [100 - 1000])
      4. Procurement Agent (Action: Supplier Allocation [0: Alpha, 1: Beta])
    """

    def __init__(self, max_steps: int = 24):
        self.max_steps = max_steps
        self.current_step = 0
        self.agents = ["demand_agent", "logistics_agent", "inventory_agent", "procurement_agent"]
        self.reset()

    def reset(self):
        self.current_step = 0
        self.state = {
            "node_congestion_index": 0.25,
            "current_inventory": 400,
            "safety_stock_threshold": 300,
            "pending_orders": 0,
            "disruption_active": False,
            "holding_cost_per_unit": 2.5,
            "stockout_penalty_per_unit": 15.0
        }
        logger.info("MARL Environment Reset to Initial State.")
        return self._get_obs()

    def _get_obs(self):
        """Construct observation vectors for each agent."""
        base_obs = np.array([
            self.state["node_congestion_index"],
            self.state["current_inventory"] / 1000.0,
            self.state["safety_stock_threshold"] / 1000.0,
            1.0 if self.state["disruption_active"] else 0.0
        ], dtype=np.float32)

        return {agent: base_obs for agent in self.agents}

    def step(self, action_dict: dict):
        self.current_step += 1

        # Simulate periodic disruption event at step 5
        if self.current_step == 5:
            self.state["disruption_active"] = True
            self.state["node_congestion_index"] = 0.85
            logger.warning(f"Step {self.current_step}: Disruption Event Injected (Congestion = 0.85)")

        # Unpack actions
        safety_stock_mult = action_dict.get("demand_agent", 1.2)
        route_choice = action_dict.get("logistics_agent", 0)  # 0: Sea, 1: Air
        order_qty = action_dict.get("inventory_agent", 500)

        # Environment Dynamics Simulation
        demand = np.random.randint(150, 300)
        if self.state["disruption_active"]:
            demand = int(demand * safety_stock_mult)

        unit_transit_cost = 45.0 if route_choice == 1 else 12.0

        # Update Inventory
        self.state["current_inventory"] -= demand
        
        # Calculate Reward Penalties (Holding Cost vs. Stockout Deficit)
        holding_cost = max(0, self.state["current_inventory"]) * self.state["holding_cost_per_unit"]
        stockout_cost = max(0, -self.state["current_inventory"]) * self.state["stockout_penalty_per_unit"]
        transit_cost = order_qty * unit_transit_cost

        total_step_cost = holding_cost + stockout_cost + transit_cost
        shared_reward = -1.0 * (total_step_cost / 1000.0)  # Minimize total operational expenditure

        done = self.current_step >= self.max_steps

        info = {
            "step": self.current_step,
            "demand": demand,
            "inventory_after_demand": self.state["current_inventory"],
            "holding_cost": holding_cost,
            "stockout_cost": stockout_cost,
            "transit_cost": transit_cost,
            "step_reward": shared_reward
        }

        return self._get_obs(), {agent: shared_reward for agent in self.agents}, done, info


if __name__ == "__main__":
    logger.info("Initializing ExoChain MARL Environment Test Run...")
    env = SupplyChainMARLEnv(max_steps=10)
    obs = env.reset()

    for step in range(1, 11):
        # Sample joint policy actions
        actions = {
            "demand_agent": 1.35,
            "logistics_agent": 1 if step >= 5 else 0, # Shift to Air express on disruption
            "inventory_agent": 500,
            "procurement_agent": 0
        }

        obs, rewards, done, info = env.step(actions)
        print(f"Step {step:02d} | Inv: {info['inventory_after_demand']:>5} | Reward: {rewards['demand_agent']:>7.2f} | Disruption: {env.state['disruption_active']}")
