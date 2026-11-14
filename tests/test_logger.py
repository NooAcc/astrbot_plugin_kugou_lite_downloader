"""插件日志必须接入 AstrBot 的统一日志体系。"""

import importlib
import logging

import pytest


@pytest.mark.parametrize(
    "module_name",
    ["main", "kugou_client", "file_cache", "tagger", "kugou_login", "metadata"],
)
def test_plugin_logs_use_astrbot_logger(module_name, caplog):
    module = importlib.import_module(module_name)

    with caplog.at_level(logging.WARNING, logger="astrbot"):
        module.logger.warning("[kugou] 日志路由回归")

    assert len(caplog.records) == 1
    assert caplog.records[0].name == "astrbot"
    assert caplog.records[0].getMessage() == "[kugou] 日志路由回归"
