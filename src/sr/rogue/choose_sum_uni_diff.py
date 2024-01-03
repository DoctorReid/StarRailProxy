from typing import ClassVar, Optional

from cv2.typing import MatLike

from basic import Point, Rect, str_utils
from basic.i18_utils import gt
from basic.img import cv2_utils
from sr.context import Context
from sr.image.sceenshot.screen_state import in_secondary_ui, ScreenState
from sr.operation import Operation, OperationOneRoundResult


class ChooseSimUniDiff(Operation):

    DIFF_POINT_MAP: ClassVar[dict[int, Point]] = {
        1: Point(0, 0),
        2: Point(0, 0),
        3: Point(0, 0),
        4: Point(0, 0),
        5: Point(0, 0),
    }

    def __init__(self, ctx: Context, num: int):
        """
        需要在模拟宇宙入口页面中使用 且先选择了普通模拟宇宙
        选择对应的难度
        :param ctx:
        :param num: 难度 支持 1~5
        """
        super().__init__(ctx, try_times=5,
                         op_name='%s %s %d' % (gt('模拟宇宙', 'ui'), gt('选择难度', 'ui'), num))

        self.num: int = num

    def _execute_one_round(self) -> OperationOneRoundResult:
        screen: MatLike = self.screenshot()

        if not in_secondary_ui(screen, self.ctx.ocr, ScreenState.SIM_TYPE_NORMAL.value):
            return Operation.round_retry('未在模拟宇宙页面', wait=1)

        if not self.ctx.controller.click(ChooseSimUniDiff.DIFF_POINT_MAP[self.num]):
            return Operation.round_retry('点击难度失败', wait=1)

        return Operation.round_success()
