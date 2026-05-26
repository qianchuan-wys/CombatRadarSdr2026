"""
改进的干扰波密钥生成器实现方案
路径: copilot/combat_radar_sdr/protocol_jam_key.py

此模块提供 0x0A06 JAM-Wave 命令的密钥生成功能，
支持多种密钥策略和密钥轮换机制。
"""

from __future__ import annotations

import time
import random
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class JamKeyMode(Enum):
    """密钥生成模式"""
    STATIC = "static"  # 固定密钥
    RANDOM = "random"  # 随机密钥（每次不同）
    PERIODIC = "periodic"  # 周期性轮换
    TIME_BASED = "time-based"  # 基于系统时间


class RobotType(Enum):
    """RM2026 机器人类型映射"""
    HERO = 0
    ENGINEER = 1
    INFANTRY = 2
    DRONE = 3
    SENTINEL = 4


@dataclass
class JamKeyConfig:
    """干扰密钥配置"""
    mode: JamKeyMode = JamKeyMode.RANDOM
    sdr_behavior: int = 0  # 对应机器人类型或干扰模式
    team_id: int = 1  # 队伍 ID（0-255）
    seed: int = 2026  # 随机种子
    key_rotate_hz: float = 1.0  # 密钥轮换频率（Hz）
    key_min_val: int = 0  # 密钥最小值（0 或 0，取决于规则）
    key_max_val: int = 255  # 密钥最大值（10 或 255，取决于规则）
    

class JamKeyGenerator:
    """
    RM2026 干扰波密钥生成器
    
    生成遵循协议规范的 7 字节密钥数据：
    sdr_behavior(1B) + key_1(1B) + key_2(1B) + key_3(1B) + 
                       key_4(1B) + key_5(1B) + key_6(1B)
    
    支持 4 种生成模式：
    - STATIC: 固定密钥，不变
    - RANDOM: 纯随机密钥序列
    - PERIODIC: 按频率周期轮换
    - TIME_BASED: 基于系统时间戳
    """
    
    def __init__(self, config: JamKeyConfig):
        """
        初始化密钥生成器
        
        Args:
            config: JamKeyConfig 配置对象
        """
        self.config = config
        self.rng = random.Random(config.seed)
        self.counter = 0
        self.last_rotation_time = time.time()
        self.current_key: Optional[bytes] = None
        
        # 初始化静态密钥（仅用于 STATIC 模式）
        if config.mode == JamKeyMode.STATIC:
            self.current_key = self._generate_random_key()
    
    def _generate_random_key(self) -> bytes:
        """生成一个随机 7 字节密钥"""
        key_data = bytearray()
        # sdr_behavior
        key_data.append(self.config.sdr_behavior & 0xFF)
        # key_1-6
        for _ in range(6):
            val = self.rng.randint(self.config.key_min_val, self.config.key_max_val)
            key_data.append(val & 0xFF)
        return bytes(key_data)
    
    def _generate_periodic_key(self, sim_time_s: float) -> bytes:
        """
        基于时间的周期性密钥轮换
        
        在指定频率下更新密钥。如果 key_rotate_hz=1.0，则每秒轮换一次。
        """
        if self.config.key_rotate_hz <= 0:
            return self.current_key or self._generate_random_key()
        
        rotation_period = 1.0 / self.config.key_rotate_hz
        time_since_last = sim_time_s - self.last_rotation_time
        
        if time_since_last >= rotation_period:
            # 触发轮换
            self.current_key = self._generate_random_key()
            self.last_rotation_time = sim_time_s
            self.counter += 1
        
        return self.current_key or self._generate_random_key()
    
    def _generate_time_based_key(self, sim_time_s: float) -> bytes:
        """
        基于系统时间戳的密钥生成
        
        使用当前时间戳作为密钥的一部分，实现时间同步验证。
        """
        key_data = bytearray()
        
        # sdr_behavior
        key_data.append(self.config.sdr_behavior & 0xFF)
        
        # 时间戳相关
        current_time = int(sim_time_s) & 0xFFFFFFFF
        
        # key_1: 队伍 ID
        key_data.append(self.config.team_id & 0xFF)
        
        # key_2-3: 时间戳低 16 位
        key_data.append((current_time >> 8) & 0xFF)
        key_data.append((current_time >> 16) & 0xFF)
        
        # key_4: 模型周期计数（同时提供不确定性）
        key_data.append(self.counter & 0xFF)
        
        # key_5: 随机挑战
        key_data.append(self.rng.randint(self.config.key_min_val, self.config.key_max_val) & 0xFF)
        
        # key_6: 校验字节（简单 XOR）
        xor_val = 0
        for b in key_data:
            xor_val ^= b
        key_data.append(xor_val & 0xFF)
        
        self.counter += 1
        return bytes(key_data)
    
    def generate(self, sim_time_s: float = 0.0) -> bytes:
        """
        生成 7 字节的干扰密钥
        
        Args:
            sim_time_s: 仿真时间（秒），用于周期轮换和时间戳模式
        
        Returns:
            7 字节的密钥数据
        """
        if self.config.mode == JamKeyMode.STATIC:
            return self.current_key or self._generate_random_key()
        
        elif self.config.mode == JamKeyMode.RANDOM:
            return self._generate_random_key()
        
        elif self.config.mode == JamKeyMode.PERIODIC:
            return self._generate_periodic_key(sim_time_s)
        
        elif self.config.mode == JamKeyMode.TIME_BASED:
            return self._generate_time_based_key(sim_time_s)
        
        else:
            # 默认随机
            return self._generate_random_key()
    
    def get_current_key_str(self) -> str:
        """获取当前密钥的十六进制字符串表示"""
        key = self.current_key or self.generate()
        return key.hex().upper()
    
    def rotate(self) -> bytes:
        """强制进行一次密钥轮换"""
        self.current_key = self._generate_random_key()
        self.counter += 1
        return self.current_key
    
    def get_stats(self) -> dict:
        """获取生成器统计信息"""
        return {
            "mode": self.config.mode.value,
            "sdr_behavior": self.config.sdr_behavior,
            "team_id": self.config.team_id,
            "counter": self.counter,
            "key_rotate_hz": self.config.key_rotate_hz,
            "last_rotation_time": self.last_rotation_time,
        }


# ============================================================================
# 集成到 protocol.py 的改进
# ============================================================================

"""
建议在 protocol.py 中添加以下内容：

from protocol_jam_key import (
    JamKeyGenerator, 
    JamKeyConfig, 
    JamKeyMode,
    RobotType,
)

# 便利函数
def build_jam_frame(jam_key_data: bytes, seq: int) -> bytes:
    '''构建 0x0A06 JAM-Wave 帧
    
    Args:
        jam_key_data: 7 字节密钥数据
        seq: 序列号
    
    Returns:
        完整的协议帧（含 SOF/len/seq/crc8/cmd_id/data/crc16）
    '''
    return build_referee_frame(CMD_0A06, jam_key_data, seq)
"""


# ============================================================================
# 改进后的 jam_tx_app.py 使用示例
# ============================================================================

"""
改进后的 jam_tx_app.py 中的关键改动：

```python
import argparse
from protocol_jam_key import JamKeyGenerator, JamKeyConfig, JamKeyMode

def main() -> int:
    parser = argparse.ArgumentParser(description="RM2026 2-GFSK jam TX (0x0A06)")
    
    # 密钥配置参数
    parser.add_argument("--jam-key-mode", 
                        choices=["static", "random", "periodic", "time-based"],
                        default="random",
                        help="密钥生成模式")
    parser.add_argument("--jam-sdr-behavior", type=int, default=0,
                        help="sdr_behavior 字段 (0-5: hero/engineer/infantry/drone/sentinel)")
    parser.add_argument("--jam-team-id", type=int, default=1,
                        help="队伍 ID (0-255)")
    parser.add_argument("--jam-key-rotate-hz", type=float, default=1.0,
                        help="密钥轮换频率 (Hz)，仅用于 periodic 模式")
    parser.add_argument("--jam-key-seed", type=int, default=2026,
                        help="随机种子")
    
    args = parser.parse_args()
    
    # 创建密钥配置
    jam_key_config = JamKeyConfig(
        mode=JamKeyMode(args.jam_key_mode),
        sdr_behavior=args.jam_sdr_behavior,
        team_id=args.jam_team_id,
        seed=args.jam_key_seed,
        key_rotate_hz=args.jam_key_rotate_hz,
        # 注意：这里可能需要根据比赛规则调整 key_min_val/key_max_val
    )
    
    key_gen = JamKeyGenerator(jam_key_config)
    
    # 在发送循环中使用
    def next_jam_key(sim_time_s: float) -> bytes:
        return key_gen.generate(sim_time_s)
    
    # ... 其他初始化逻辑 ...
```
"""


if __name__ == "__main__":
    # 演示用法
    
    print("=" * 70)
    print("JAM-Wave 密钥生成器演示")
    print("=" * 70)
    
    # 模式 1: STATIC（固定密钥）
    print("\n[模式 1] STATIC - 固定密钥")
    config1 = JamKeyConfig(
        mode=JamKeyMode.STATIC,
        sdr_behavior=0,  # Hero
        team_id=1,
    )
    gen1 = JamKeyGenerator(config1)
    for i in range(3):
        key = gen1.generate(sim_time_s=float(i))
        print(f"  时刻 {i}s: {key.hex().upper()}")
    
    # 模式 2: RANDOM（纯随机）
    print("\n[模式 2] RANDOM - 纯随机密钥")
    config2 = JamKeyConfig(
        mode=JamKeyMode.RANDOM,
        sdr_behavior=1,  # Engineer
        team_id=2,
    )
    gen2 = JamKeyGenerator(config2)
    for i in range(3):
        key = gen2.generate(sim_time_s=float(i))
        print(f"  时刻 {i}s: {key.hex().upper()}")
    
    # 模式 3: PERIODIC（周期轮换）
    print("\n[模式 3] PERIODIC - 每 2 秒轮换一次")
    config3 = JamKeyConfig(
        mode=JamKeyMode.PERIODIC,
        sdr_behavior=2,  # Infantry
        team_id=3,
        key_rotate_hz=0.5,  # 每 2 秒轮换
    )
    gen3 = JamKeyGenerator(config3)
    for i in [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]:
        key = gen3.generate(sim_time_s=i)
        print(f"  时刻 {i}s: {key.hex().upper()}")
    
    # 模式 4: TIME_BASED（基于时间戳）
    print("\n[模式 4] TIME_BASED - 时间戳同步模式")
    config4 = JamKeyConfig(
        mode=JamKeyMode.TIME_BASED,
        sdr_behavior=3,  # Drone
        team_id=4,
    )
    gen4 = JamKeyGenerator(config4)
    for i in [0.0, 1.0, 2.0, 3.0]:
        key = gen4.generate(sim_time_s=i)
        print(f"  时刻 {i}s: {key.hex().upper()}")
    
    print("\n" + "=" * 70)
    print("生成器统计信息")
    print("=" * 70)
    for idx, gen in enumerate([gen1, gen2, gen3, gen4], 1):
        stats = gen.get_stats()
        print(f"\n生成器 {idx}:")
        for key, val in stats.items():
            print(f"  {key}: {val}")
