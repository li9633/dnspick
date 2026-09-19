"""dnspick —— 为家庭网络挑选最优 DNS 上游的测量器。

设计要点：
    1. 不部署、不改系统设置，只输出测量结论（用户手动填进 SmartDNS）。
    2. 两段式：先对每个 DNS 做解析矩阵，再对解析出的 IP 做链路探测。
    3. 并发模型：海量 I/O（DNS 查询 / TCP 握手）用 asyncio；
       必须阻塞的系统调用（ICMP、traceroute、GeoIP 查询）走线程池。

Python 3.13.7+。
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
