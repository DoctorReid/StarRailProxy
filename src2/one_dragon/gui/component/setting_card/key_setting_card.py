from PySide6.QtCore import Signal, QObject
from PySide6.QtGui import QIcon, Qt
from qfluentwidgets import SettingCard, FluentIconBase, PushButton
from typing import Union, Optional

from one_dragon.base.controller.pc_button.pc_button_listener import PcButtonListener
from one_dragon.gui.component.layout_utils import IconSize, Margins
from one_dragon.gui.component.setting_card.setting_card_base import SettingCardBase
from one_dragon.gui.component.setting_card.yaml_config_adapter import YamlConfigAdapter
from one_dragon.utils.i18_utils import gt


class KeyEventWorker(QObject):

    key_pressed = Signal(str)

    def on_key_press(self, key: str) -> None:
        """
        按键后触发 发送信号
        :param key:
        :return:
        """
        self.key_pressed.emit(key)


class KeySettingCard(SettingCardBase):

    value_changed = Signal(str)

    def __init__(self, title:str,
                icon: Union[str, QIcon, FluentIconBase]=None,
                iconSize:IconSize = IconSize(16,16),
                margins:Margins = Margins(16,16,0,16), value: str = '',
                content: Optional[str] = None, parent=None,
                adapter: Optional[YamlConfigAdapter] = None
                ):
        """
        更改按键的
        :param icon: 左边显示的图标
        :param title: 左边的标题 中文
        :parma value: 当前值
        :param content: 左侧的详细文本 中文
        :param parent: 组件的parent
        :param adapter: 配置适配器 自动更新对应配置文件
        """
        SettingCardBase.__init__(self,title,icon,iconSize,margins,content, parent)
        self.adapter: YamlConfigAdapter = adapter

        self.value: str = value
        self.btn = PushButton(text=value.upper(), parent=self)
        self.btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn.clicked.connect(self._on_btn_clicked)

        self.button_listener = None  # 监听
        self.key_worker = KeyEventWorker()
        self.key_worker.key_pressed.connect(self._on_key_signal)

        self.hBoxLayout.addWidget(self.btn, 1, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

    def _on_btn_clicked(self) -> None:
        """
        点击按钮后 开始监听按键事件
        :return:
        """
        if self.button_listener is None:
            self.button_listener = PcButtonListener(self._on_key_press, listen_keyboard=True, listen_mouse=True)
            self.btn.setText(gt('请按键', 'ui'))
            self.button_listener.start()
        else:
            self._stop_listener()
            self.btn.setText(self.value.upper())

    def _on_key_press(self, key):
        """
        按键时触发
        :param key:
        :return:
        """
        self._stop_listener()
        self.key_worker.on_key_press(key)

    def _on_key_signal(self, key: str) -> None:
        """
        :param key:
        :return:
        """
        self.setValue(key)

        if self.adapter is not None:
            self.adapter.set_value(key)

    def init_with_adapter(self, adapter: YamlConfigAdapter) -> None:
        """
        初始化值
        """
        self.adapter = adapter
        self.setValue(self.adapter.get_value(), emit_signal=False)

    def setContent(self, content: str) -> None:
        """
        更新左侧详细文本
        :param content: 文本 中文
        :return:
        """
        SettingCard.setContent(self, gt(content, 'ui'))

    def setValue(self, value: str, emit_signal: bool = True) -> None:
        """
        设置值
        :param value:
        :param emit_signal: 是否发送信号
        :return:
        """
        self.btn.setText(value.upper())
        if emit_signal:
            self.value_changed.emit(value)

    def _stop_listener(self) -> None:
        """
        停止键盘鼠标的监听
        :return:
        """
        if self.button_listener is not None:
            self.button_listener.stop()
            self.button_listener = None