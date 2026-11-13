"""回归测试：插件在 AstrBot 的包路径布局下必须能被导入。

AstrBot 以 ``data.plugins.<插件目录名>.main`` 导入插件，插件目录本身不在
sys.path 上。曾经因为跨模块使用顶层绝对导入（``from file_cache import ...``）
导致线上加载失败 ``No module named 'file_cache'``；本测试复现该部署布局，
防止回归。
"""

import importlib
import logging
import os
import shutil
import sys
import types


def _install_astrbot_stub():
    """在 astrbot 不可用的环境里提供最小桩，仅用于让 main.py 走完导入链。"""
    if "astrbot" in sys.modules:
        return
    try:
        import astrbot  # noqa: F401
        return
    except ImportError:
        pass

    def make(name, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module
        return module

    class _Filter:
        def command(self, _name):
            return lambda func: func

        def permission_type(self, _value):
            return lambda func: func

    class _PermissionType:
        ADMIN = "admin"

    class _Context:
        pass

    class _Star:
        def __init__(self, context=None):
            self.context = context

    make("astrbot")
    make("astrbot.api", logger=logging.getLogger("astrbot"))
    make("astrbot.api.event", AstrMessageEvent=object, filter=_Filter())
    make("astrbot.api.event.filter", PermissionType=_PermissionType)
    make("astrbot.api.message_components", Image=object, Plain=object)
    make("astrbot.api.star", Context=_Context, Star=_Star)


def test_import_as_astrbot_package(tmp_path, monkeypatch):
    """以 data.plugins.<dir>.main 导入时不应依赖插件目录在 sys.path 上。"""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    package_name = "astrbot_plugin_kugou_music"
    plugin_dir = tmp_path / "data" / "plugins" / package_name
    plugin_dir.mkdir(parents=True)

    for filename in os.listdir(repo_root):
        if filename.endswith(".py"):
            shutil.copy(os.path.join(repo_root, filename), str(plugin_dir))

    # 模拟 AstrBot：只有 data/ 的根在 sys.path，插件目录不在。
    # conftest.py 会把仓库根加入 sys.path 以便其它测试以顶层模块方式导入；
    # 这里必须临时移除它，否则会掩盖线上真实的 ModuleNotFoundError。
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(
        sys, "path", [p for p in sys.path if os.path.abspath(p) != repo_root]
    )
    # 清掉可能已由其它测试以顶层方式缓存的模块，确保这次是真正的包内导入
    for name in list(sys.modules):
        if name.startswith("data.plugins." + package_name) or name in {
            "file_cache", "kugou_client", "kugou_login", "quality", "audio_probe",
            "main",
        }:
            monkeypatch.delitem(sys.modules, name, raising=False)

    _install_astrbot_stub()

    module = importlib.import_module(f"data.plugins.{package_name}.main")

    assert hasattr(module, "KugouMusic")
    assert hasattr(module, "AudioFetcher")
    # 同级模块必须解析为插件包内的子模块，而不是 sys.path 上的顶层模块
    assert module.CacheManager.__module__.startswith(
        f"data.plugins.{package_name}."
    )
