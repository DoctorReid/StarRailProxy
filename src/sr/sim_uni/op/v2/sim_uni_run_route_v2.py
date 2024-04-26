from typing import List, ClassVar, Optional, Callable

from cv2.typing import MatLike

from basic import Point, cal_utils
from basic.i18_utils import gt
from basic.img import cv2_utils
from basic.log_utils import log
from sr.context import Context
from sr.image.sceenshot import mini_map, screen_state, MiniMapInfo
from sr.operation import StateOperation, StateOperationEdge, StateOperationNode, OperationOneRoundResult, Operation, \
    OperationResult
from sr.operation.unit.interact import Interact
from sr.operation.unit.move import MoveWithoutPos
from sr.sim_uni.op.move_in_sim_uni import MoveToNextLevel
from sr.sim_uni.op.sim_uni_battle import SimUniEnterFight, SimUniFightElite
from sr.sim_uni.op.sim_uni_event import SimUniEvent
from sr.sim_uni.op.v2.sim_uni_move_v2 import SimUniMoveToEnemyByMiniMap, SimUniMoveToEnemyByDetect, \
    SimUniMoveToEventByDetect, delta_angle_to_detected_object
from sr.sim_uni.sim_uni_const import SimUniLevelTypeEnum
from sryolo.detector import DetectResult


class SimUniRunRouteBase(StateOperation):

    STATUS_FIGHT: ClassVar[str] = '遭遇战斗'
    STATUS_WITH_RED: ClassVar[str] = '小地图有红点'
    STATUS_NO_RED: ClassVar[str] = '小地图无红点'
    STATUS_WITH_MM_EVENT: ClassVar[str] = '小地图有事件'
    STATUS_NO_MM_EVENT: ClassVar[str] = '小地图无事件'
    STATUS_WITH_DETECT_EVENT: ClassVar[str] = '识别到事件'
    STATUS_NO_DETECT_EVENT: ClassVar[str] = '识别不到事件'
    STATUS_WITH_ENEMY: ClassVar[str] = '识别到敌人'
    STATUS_NO_ENEMY: ClassVar[str] = '识别不到敌人'
    STATUS_WITH_ENTRY: ClassVar[str] = '识别到下层入口'
    STATUS_NO_ENTRY: ClassVar[str] = '识别不到下层入口'
    STATUS_NOTHING: ClassVar[str] = '识别不到任何内容'

    def __init__(self, ctx: Context, op_name: str, try_times: int = 2,
                 nodes: Optional[List[StateOperationNode]] = None,
                 edges: Optional[List[StateOperationEdge]] = None,
                 specified_start_node: Optional[StateOperationNode] = None,
                 timeout_seconds: float = -1,
                 op_callback: Optional[Callable[[OperationResult], None]] = None):
        StateOperation.__init__(self,
                                ctx=ctx, op_name=op_name, try_times=try_times,
                                nodes=nodes, edges=edges, specified_start_node=specified_start_node,
                                timeout_seconds=timeout_seconds, op_callback=op_callback)

        self.moved_to_target: bool = False  # 是否已经产生了朝向目标的移动
        self.nothing_times: int = 0  # 识别不到任何内容的次数
        self.previous_angle: float = 0  # 之前的朝向 识别到目标时应该记录下来 后续可以在这个方向附近找下一个目标

    def _check_angle(self, screen: Optional[MatLike] = None) -> OperationOneRoundResult:
        """
        检测并更新角度
        :return:
        """
        if screen is None:
            screen = self.screenshot()
        mm = mini_map.cut_mini_map(screen, self.ctx.game_config.mini_map_pos)
        self.previous_angle = mini_map.analyse_angle(mm)
        return Operation.round_success()

    def _turn_to_previous_angle(self, screen: Optional[MatLike] = None) -> OperationOneRoundResult:
        """
        战斗后的处理 先转到原来的朝向 再取找下一个目标
        :return:
        """
        if screen is None:
            screen = self.screenshot()
        mm = mini_map.cut_mini_map(screen, self.ctx.game_config.mini_map_pos)
        angle = mini_map.analyse_angle(mm)
        self.ctx.controller.turn_from_angle(angle, self.previous_angle)
        return Operation.round_success()

    def _check_next_entry(self) -> OperationOneRoundResult:
        """
        找下层入口 主要判断能不能找到
        :return:
        """
        self._view_up()
        screen: MatLike = self.screenshot()
        entry_list = MoveToNextLevel.get_next_level_type(screen, self.ctx.ih)
        if len(entry_list) == 0:
            return Operation.round_success(status=SimUniRunRouteBase.STATUS_NO_ENTRY)
        else:
            self.nothing_times = 0
            return Operation.round_success(status=SimUniRunRouteBase.STATUS_WITH_ENTRY)

    def _move_to_next(self):
        """
        朝下层移动
        :return:
        """
        self.nothing_times = 0
        self.moved_to_target = True
        op = MoveToNextLevel(self.ctx, level_type=SimUniLevelTypeEnum.COMBAT.value)
        return Operation.round_by_op(op.execute())

    def _turn_when_nothing(self) -> OperationOneRoundResult:
        """
        当前画面识别不到任何内容时候 转动一下
        :return:
        """
        if not self.moved_to_target:
            # 还没有产生任何移动的情况下 又识别不到任何内容 则可能是距离较远导致。先尝试往前走1秒
            self.ctx.controller.move('w', 1)
        self.nothing_times += 1
        if self.nothing_times >= 12:
            return Operation.round_fail(SimUniRunRouteBase.STATUS_NOTHING)

        # angle = (25 + 10 * self.nothing_times) * (1 if self.nothing_times % 2 == 0 else -1)  # 来回转动视角
        # 由于攻击之后 人物可能朝反方向了 因此要转动多一点
        # 不要被360整除 否则转一圈之后还是被人物覆盖了看不到
        angle = 35
        self.ctx.controller.turn_by_angle(angle)
        return Operation.round_success(wait=0.5)

    def _view_down(self):
        """
        视角往下移动 方便识别目标
        :return:
        """
        if self.ctx.detect_info.view_down:
            return
        self.ctx.controller.turn_down(25)
        self.ctx.detect_info.view_down = True

    def _view_up(self):
        """
        视角往上移动 恢复原来的视角
        :return:
        """
        if not self.ctx.detect_info.view_down:
            return
        self.ctx.controller.turn_down(-25)
        self.ctx.detect_info.view_down = False


class SimUniRunCombatRouteV2(SimUniRunRouteBase):

    def __init__(self, ctx: Context):
        """
        区域-战斗
        1. 检测地图是否有红点
        2. 如果有红点 移动到最近的红点 并进行攻击。攻击一次后回到步骤1判断。
        3. 如果没有红点 识别敌对物种位置，向最大的移动，并进行攻击。攻击一次后回到步骤1判断。
        4. 如果没有红点也没有识别到敌对物种，检测下层入口位置，发现后进入下层移动。未发现则选择视角返回步骤1判断。
        """
        edges: List[StateOperationEdge] = []

        first_angle = StateOperationNode('第一次记录角度', self._check_angle)

        check = StateOperationNode('画面检测', self._check_screen)
        edges.append(StateOperationEdge(first_angle, check))

        # 小地图有红点 就按红点移动
        move_by_red = StateOperationNode('向红点移动', self._move_by_red)
        edges.append(StateOperationEdge(check, move_by_red, status=SimUniRunRouteBase.STATUS_WITH_RED))

        # 小地图没有红点 就在画面上找敌人
        detect_enemy = StateOperationNode('识别敌人', self._detect_enemy_in_screen)
        edges.append(StateOperationEdge(check, detect_enemy, status=SimUniRunRouteBase.STATUS_NO_RED))
        # 找到了敌人就开始移动
        move_by_detect = StateOperationNode('向敌人移动', self._move_by_detect)
        edges.append(StateOperationEdge(detect_enemy, move_by_detect, status=SimUniRunRouteBase.STATUS_WITH_ENEMY))

        # 到达后开始战斗
        fight = StateOperationNode('进入战斗', self._enter_fight)
        edges.append(StateOperationEdge(move_by_red, fight, status=SimUniMoveToEnemyByMiniMap.STATUS_ARRIVAL))
        # 进行了战斗 就重新开始
        after_fight = StateOperationNode('战斗后处理', self._turn_to_previous_angle)
        edges.append(StateOperationEdge(fight, after_fight))
        edges.append(StateOperationEdge(after_fight, check))
        # 其它可能会进入战斗的情况
        edges.append(StateOperationEdge(check, after_fight, status=SimUniRunRouteBase.STATUS_FIGHT))
        edges.append(StateOperationEdge(move_by_red, after_fight, status=SimUniMoveToEnemyByMiniMap.STATUS_FIGHT))
        edges.append(StateOperationEdge(move_by_detect, after_fight, status=SimUniMoveToEnemyByDetect.STATUS_FIGHT))

        # 画面上也找不到敌人 就找下层入口
        check_entry = StateOperationNode('识别下层入口', self._check_next_entry)
        edges.append(StateOperationEdge(detect_enemy, check_entry, status=SimUniRunRouteBase.STATUS_NO_ENEMY))
        # 找到了下层入口就开始移动
        move_to_next = StateOperationNode('向下层移动', self._move_to_next)
        edges.append(StateOperationEdge(check_entry, move_to_next, status=SimUniRunRouteBase.STATUS_WITH_ENTRY))
        # 找不到下层入口就转向找目标
        turn = StateOperationNode('转动找目标', self._turn_when_nothing)
        edges.append(StateOperationEdge(check_entry, turn, status=SimUniRunRouteBase.STATUS_NO_ENTRY))
        # 转动完重新开始目标识别
        edges.append(StateOperationEdge(turn, check))

        super().__init__(ctx,
                         op_name=gt('区域-战斗', 'ui'),
                         edges=edges,
                         specified_start_node=first_angle)

        self.last_state: str = ''  # 上一次的画面状态
        self.current_state: str = ''  # 这一次的画面状态

    def _check_screen(self) -> OperationOneRoundResult:
        """
        检测屏幕
        :return:
        """
        screen = self.screenshot()

        # 为了保证及时攻击 外层仅判断是否在大世界画面 非大世界画面时再细分处理
        self.current_state = screen_state.get_sim_uni_screen_state(
            screen, self.ctx.im, self.ctx.ocr,
            in_world=True, battle=True)
        log.debug('当前画面 %s', self.current_state)

        if self.current_state == screen_state.ScreenState.NORMAL_IN_WORLD.value:
            return self._handle_in_world(screen)
        else:
            return self._handle_not_in_world(screen)

    def _handle_in_world(self, screen: MatLike) -> OperationOneRoundResult:
        mm = mini_map.cut_mini_map(screen, self.ctx.game_config.mini_map_pos)
        mm_info: MiniMapInfo = mini_map.analyse_mini_map(mm)

        if mini_map.is_under_attack_new(mm_info):
            op = SimUniEnterFight(self.ctx)
            op_result = op.execute()
            if op_result.success:
                return Operation.round_success(status=SimUniRunRouteBase.STATUS_FIGHT)
            else:
                return Operation.round_by_op(op_result)

        pos, _ = mini_map.get_closest_enemy_pos(mm_info)

        if pos is None:
            return Operation.round_success(status=SimUniRunRouteBase.STATUS_NO_RED)
        else:
            self.previous_angle = cal_utils.get_angle_by_pts(Point(0, 0), pos)  # 记录有目标的方向
            if self.ctx.one_dragon_config.is_debug:  # 红点已经比较成熟 调试时强制使用yolo
                return Operation.round_success(status=SimUniRunRouteBase.STATUS_NO_RED)
            return Operation.round_success(status=SimUniRunRouteBase.STATUS_WITH_RED)

    def _move_by_red(self) -> OperationOneRoundResult:
        """
        朝小地图红点走去
        :return:
        """
        self.nothing_times = 0
        self.moved_to_target = True
        op = SimUniMoveToEnemyByMiniMap(self.ctx)
        return Operation.round_by_op(op.execute())

    def _enter_fight(self) -> OperationOneRoundResult:
        op = SimUniEnterFight(self.ctx,
                              first_state=screen_state.ScreenState.NORMAL_IN_WORLD.value,
                              )
        return op.round_by_op(op.execute())

    def _handle_not_in_world(self, screen: MatLike) -> OperationOneRoundResult:
        """
        不在大世界的场景 无论是什么 都可以交给 SimUniEnterFight 处理
        :param screen:
        :return:
        """
        op = SimUniEnterFight(self.ctx, config=self.ctx.sim_uni_challenge_config)
        op_result = op.execute()
        if op_result.success:
            return Operation.round_success(status=SimUniRunRouteBase.STATUS_FIGHT)
        else:
            return Operation.round_by_op(op_result)

    def _detect_enemy_in_screen(self) -> OperationOneRoundResult:
        """
        没有红点时 判断当前画面是否有怪
        TODO 之后可以把入口识别也放到这里
        :return:
        """
        self._view_down()
        screen: MatLike = self.screenshot()

        self.ctx.init_yolo()
        detect_results: List[DetectResult] = self.ctx.yolo.detect(screen)

        with_enemy: bool = False
        delta_angle: float = 0
        cnt: int = 0
        for result in detect_results:
            if result.detect_class.class_cate == '普通怪':
                with_enemy = True
                delta_angle += delta_angle_to_detected_object(result)
                cnt += 1

        if with_enemy:
            avg_delta_angle = delta_angle / cnt
            self.previous_angle = cal_utils.angle_add(self.previous_angle, avg_delta_angle)
            return Operation.round_success(status=SimUniRunRouteBase.STATUS_WITH_ENEMY)
        else:
            cv2_utils.show_image(screen, win_name='_detect_enemy_in_screen')
            return Operation.round_success(SimUniRunRouteBase.STATUS_NO_ENEMY)

    def _move_by_detect(self) -> OperationOneRoundResult:
        """
        识别到敌人人开始移动
        :return:
        """
        self.nothing_times = 0
        self.moved_to_target = True
        op = SimUniMoveToEnemyByDetect(self.ctx)
        return Operation.round_by_op(op.execute())


class SimUniRunEliteRouteV2(SimUniRunRouteBase):

    def __init__(self, ctx: Context):
        """
        区域-精英
        1. 检查小地图是否有红点 有就向红点移动
        2. 开怪
        3. 领取奖励
        4. 朝下层移动
        :param ctx:
        """
        edges: List[StateOperationEdge] = []

        first_angle = StateOperationNode('第一次记录角度', self._check_angle)

        check_red = StateOperationNode('识别小地图红点', self._check_red)
        edges.append(StateOperationEdge(first_angle, check_red))

        # 有红点就靠红点移动
        move_by_red = StateOperationNode('向红点移动', self._move_by_red)
        edges.append(StateOperationEdge(check_red, move_by_red, status=SimUniRunRouteBase.STATUS_WITH_RED))

        # 到达精英怪旁边发起攻击
        start_fight = StateOperationNode('进入战斗', self._enter_fight)
        edges.append(StateOperationEdge(move_by_red, start_fight, status=SimUniMoveToEnemyByMiniMap.STATUS_ARRIVAL))

        # 战斗后处理
        after_fight = StateOperationNode('战斗后处理', self._turn_to_previous_angle)
        edges.append(StateOperationEdge(start_fight, after_fight))

        # TODO 暂时没有领取奖励处理

        # 战斗后 识别下层入口
        check_entry = StateOperationNode('识别下层入口', self._check_next_entry)
        edges.append(StateOperationEdge(after_fight, check_entry))
        # 没红点时 识别下层入口
        edges.append(StateOperationEdge(check_red, check_entry, status=SimUniRunRouteBase.STATUS_NO_RED))
        # 找到了下层入口就开始移动
        move_to_next = StateOperationNode('向下层移动', self._move_to_next)
        edges.append(StateOperationEdge(check_entry, move_to_next, status=SimUniRunRouteBase.STATUS_WITH_ENTRY))
        # 找不到下层入口就转向找目标 重新开始
        turn = StateOperationNode('转动找目标', self._turn_when_nothing)
        edges.append(StateOperationEdge(check_entry, turn, status=SimUniRunRouteBase.STATUS_NO_ENTRY))
        edges.append(StateOperationEdge(turn, check_red))

        super().__init__(ctx,
                         op_name=gt('区域-精英', 'ui'),
                         edges=edges,
                         specified_start_node=check_red
                         )

    def _check_red(self) -> OperationOneRoundResult:
        """
        检查小地图是否有红点
        :return:
        """
        screen = self.screenshot()
        mm = mini_map.cut_mini_map(screen, self.ctx.game_config.mini_map_pos)
        mm_info = mini_map.analyse_mini_map(mm)
        pos_list = mini_map.get_enemy_pos(mm_info)
        if len(pos_list) == 0:
            return Operation.round_success(status=SimUniRunRouteBase.STATUS_NO_RED)
        else:
            return Operation.round_success(status=SimUniRunRouteBase.STATUS_WITH_RED)

    def _move_by_red(self) -> OperationOneRoundResult:
        """
        往小地图红点移动
        :return:
        """
        self.nothing_times = 0
        self.moved_to_target = True
        op = SimUniMoveToEnemyByMiniMap(self.ctx, no_attack=True, stop_after_arrival=True)
        return Operation.round_by_op(op.execute())

    def _enter_fight(self) -> OperationOneRoundResult:
        """
        移动到精英怪旁边之后 发起攻击
        :return:
        """
        op = SimUniFightElite(self.ctx)
        return Operation.round_by_op(op.execute())


class SimUniRunEventRouteV2(SimUniRunRouteBase):

    def __init__(self, ctx: Context):
        """
        区域-事件
        1. 识别小地图上是否有事件图标 有的话就移动
        2. 小地图没有事件图标时 识别画面上是否有事件牌 有的话移动
        3. 交互
        4. 进入下一层
        :param ctx:
        """
        edges: List[StateOperationEdge] = []

        # 小地图有事件的话就走小地图
        check_mm = StateOperationNode('识别小地图事件', self._check_mm_icon)
        move_by_mm = StateOperationNode('按小地图朝事件移动', self._move_by_mm)
        edges.append(StateOperationEdge(check_mm, move_by_mm, status=SimUniRunRouteBase.STATUS_WITH_MM_EVENT))

        # 小地图没有事件的话就靠识别
        detect_event = StateOperationNode('识别画面事件', self._detect_event_in_screen)
        edges.append(StateOperationEdge(check_mm, detect_event, status=SimUniRunRouteBase.STATUS_NO_MM_EVENT))
        # 识别到就移动
        move_by_detect = StateOperationNode('按画面朝事件移动', self._move_by_detect)
        edges.append(StateOperationEdge(detect_event, move_by_detect, status=SimUniRunRouteBase.STATUS_WITH_DETECT_EVENT))

        # 走到了就进行交互 进入这里代码已经识别到事件了 则必须要交互才能进入下一层
        interact = StateOperationNode('交互', self._interact)
        edges.append(StateOperationEdge(move_by_mm, interact))
        edges.append(StateOperationEdge(move_by_detect, interact, status=SimUniMoveToEventByDetect.STATUS_ARRIVAL))

        # 交互了之后开始事件判断
        event = StateOperationNode('事件', self._handle_event)
        edges.append(StateOperationEdge(interact, event))

        # 事件之后 识别下层入口
        check_entry = StateOperationNode('识别下层入口', self._check_next_entry)
        edges.append(StateOperationEdge(event, check_entry))
        # 识别不到事件 也识别下层入口
        edges.append(StateOperationEdge(detect_event, check_entry, status=SimUniRunRouteBase.STATUS_NO_DETECT_EVENT))
        # 找到了下层入口就开始移动
        move_to_next = StateOperationNode('向下层移动', self._move_to_next)
        edges.append(StateOperationEdge(check_entry, move_to_next, status=SimUniRunRouteBase.STATUS_WITH_ENTRY))
        # 找不到下层入口就转向找目标 重新开始
        turn = StateOperationNode('转动找目标', self._turn_when_nothing)
        edges.append(StateOperationEdge(check_entry, turn, status=SimUniRunRouteBase.STATUS_NO_ENTRY))
        edges.append(StateOperationEdge(turn, check_mm))

        super().__init__(ctx,
                         op_name=gt('区域-事件', 'ui'),
                         edges=edges,
                         specified_start_node=check_mm
                         )

        self.mm_icon_pos: Optional[Point] = None  # 小地图上事件的坐标

    def _check_mm_icon(self) -> OperationOneRoundResult:
        """
        识别小地图上的事件图标
        :return:
        """
        screen = self.screenshot()
        mm = mini_map.cut_mini_map(screen, self.ctx.game_config.mini_map_pos)
        mrl = self.ctx.im.match_template(mm, template_id='mm_sp_event')
        if mrl.max is not None:
            self.mm_icon_pos = mrl.max.center
            return Operation.round_success(status=SimUniRunRouteBase.STATUS_WITH_MM_EVENT)
        else:
            return Operation.round_success(status=SimUniRunRouteBase.STATUS_NO_MM_EVENT)

    def _move_by_mm(self) -> OperationOneRoundResult:
        """
        按小地图的图标位置机械移动
        :return:
        """
        self.nothing_times = 0
        self.moved_to_target = True
        r = self.ctx.game_config.mini_map_pos.r
        op = MoveWithoutPos(self.ctx, start=Point(r, r), target=self.mm_icon_pos)
        return Operation.round_by_op(op.execute())

    def _detect_event_in_screen(self) -> OperationOneRoundResult:
        """
        识别游戏画面上是否有事件牌
        :return:
        """
        self._view_down()
        screen = self.screenshot()

        detect_results: List[DetectResult] = self.ctx.yolo.detect(screen)

        with_event: bool = False
        for result in detect_results:
            if result.detect_class.class_name == '模拟宇宙事件':
                with_event = True
                break

        if with_event:
            return Operation.round_success(status=SimUniRunRouteBase.STATUS_WITH_DETECT_EVENT)
        else:
            cv2_utils.show_image(screen, win_name='_detect_event_in_screen')
            return Operation.round_success(SimUniRunRouteBase.STATUS_NO_DETECT_EVENT)

    def _move_by_detect(self) -> OperationOneRoundResult:
        """
        根据画面识别结果走向事件
        :return:
        """
        self.nothing_times = 0
        self.moved_to_target = True
        op = SimUniMoveToEventByDetect(self.ctx)
        return Operation.round_by_op(op.execute())

    def _interact(self) -> OperationOneRoundResult:
        """
        尝试交互
        :return:
        """
        op = Interact(self.ctx, '事件', lcs_percent=0.1, single_line=True)
        return Operation.round_by_op(op.execute())

    def _handle_event(self) -> OperationOneRoundResult:
        """
        事件处理
        :return:
        """
        op = SimUniEvent(self.ctx, skip_first_screen_check=False)
        return Operation.round_by_op(op.execute())


class SimUniRunRespiteRouteV2(SimUniRunRouteBase):

    def __init__(self, ctx: Context):
        edges: List[StateOperationEdge] = []

        # 小地图有事件的话就走小地图
        check_mm = StateOperationNode('识别小地图黑塔', self._check_mm_icon)
        move_by_mm = StateOperationNode('按小地图朝黑塔移动', self._move_by_mm)
        edges.append(StateOperationEdge(check_mm, move_by_mm, status=SimUniRunRouteBase.STATUS_WITH_MM_EVENT))

        # 小地图没有事件的话就靠识别
        detect_event = StateOperationNode('识别画面黑塔', self._detect_screen)
        edges.append(StateOperationEdge(check_mm, detect_event, status=SimUniRunRouteBase.STATUS_NO_MM_EVENT))
        # 识别到就移动
        move_by_detect = StateOperationNode('按画面朝黑塔移动', self._move_by_detect)
        edges.append(StateOperationEdge(detect_event, move_by_detect, status=SimUniRunRouteBase.STATUS_WITH_DETECT_EVENT))

        # 走到了就进行交互
        interact = StateOperationNode('交互', self._interact)
        edges.append(StateOperationEdge(move_by_mm, interact))
        edges.append(StateOperationEdge(move_by_detect, interact, status=SimUniMoveToEventByDetect.STATUS_ARRIVAL))

        # 交互了之后开始事件判断
        event = StateOperationNode('黑塔', self._handle_event)
        edges.append(StateOperationEdge(interact, event))

        # 事件之后 识别下层入口
        check_entry = StateOperationNode('识别下层入口', self._check_next_entry)
        edges.append(StateOperationEdge(event, check_entry))
        # 识别不到事件、交互失败 也识别下层入口
        edges.append(StateOperationEdge(detect_event, check_entry, status=SimUniRunRouteBase.STATUS_NO_DETECT_EVENT))
        edges.append(StateOperationEdge(interact, check_entry, success=False))
        # 找到了下层入口就开始移动
        move_to_next = StateOperationNode('向下层移动', self._move_to_next)
        edges.append(StateOperationEdge(check_entry, move_to_next, status=SimUniRunRouteBase.STATUS_WITH_ENTRY))
        # 找不到下层入口就转向找目标 重新开始
        turn = StateOperationNode('转动找目标', self._turn_when_nothing)
        edges.append(StateOperationEdge(check_entry, turn, status=SimUniRunRouteBase.STATUS_NO_ENTRY))
        edges.append(StateOperationEdge(turn, check_mm))

        super().__init__(ctx,
                         op_name=gt('区域-事件', 'ui'),
                         edges=edges,
                         specified_start_node=check_mm
                         )

        self.mm_icon_pos: Optional[Point] = None  # 小地图上黑塔的坐标

    def _check_mm_icon(self) -> OperationOneRoundResult:
        """
        识别小地图上的黑塔图标
        :return:
        """
        screen = self.screenshot()
        mm = mini_map.cut_mini_map(screen, self.ctx.game_config.mini_map_pos)
        mrl = self.ctx.im.match_template(mm, template_id='mm_sp_herta')
        if mrl.max is not None:
            self.mm_icon_pos = mrl.max.center
            return Operation.round_success(status=SimUniRunRouteBase.STATUS_WITH_MM_EVENT)
        else:
            return Operation.round_success(status=SimUniRunRouteBase.STATUS_NO_MM_EVENT)

    def _move_by_mm(self) -> OperationOneRoundResult:
        """
        按小地图的图标位置机械移动
        :return:
        """
        self.nothing_times = 0
        self.moved_to_target = True
        r = self.ctx.game_config.mini_map_pos.r
        op = MoveWithoutPos(self.ctx, start=Point(r, r), target=self.mm_icon_pos)
        return Operation.round_by_op(op.execute())

    def _detect_screen(self) -> OperationOneRoundResult:
        """
        识别游戏画面上是否有事件牌
        :return:
        """
        self._view_down()
        screen = self.screenshot()

        detect_results: List[DetectResult] = self.ctx.yolo.detect(screen)

        with_event: bool = False
        for result in detect_results:
            if result.detect_class.class_name == '模拟宇宙黑塔':
                with_event = True
                break

        if with_event:
            return Operation.round_success(status=SimUniRunRouteBase.STATUS_WITH_DETECT_EVENT)
        else:
            cv2_utils.show_image(screen, win_name='_detect_event_in_screen')
            return Operation.round_success(SimUniRunRouteBase.STATUS_NO_DETECT_EVENT)

    def _move_by_detect(self) -> OperationOneRoundResult:
        """
        根据画面识别结果走向事件
        :return:
        """
        self.nothing_times = 0
        self.moved_to_target = True
        op = SimUniMoveToEventByDetect(self.ctx)
        return Operation.round_by_op(op.execute())

    def _interact(self) -> OperationOneRoundResult:
        """
        尝试交互
        :return:
        """
        op = Interact(self.ctx, '黑塔', lcs_percent=0.1, single_line=True)
        return Operation.round_by_op(op.execute())

    def _handle_event(self) -> OperationOneRoundResult:
        """
        事件处理
        :return:
        """
        op = SimUniEvent(self.ctx, skip_first_screen_check=False)
        return Operation.round_by_op(op.execute())