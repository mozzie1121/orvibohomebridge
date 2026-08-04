"""Local-area transport for the fused ORVIBO integration (Stage 1).

从 orvibo-lan-control 移植：UDP 网关发现、TCP 网关连接、网关生命周期管理、
严格封包编解码与日志脱敏。当前只接入"状态接收"链路，控制路由在阶段 2 接入。
"""

from .gateway_manager import GatewayManager
from .privacy import mask_host, mask_identifier

__all__ = ["GatewayManager", "mask_host", "mask_identifier"]
