import time

from cv2.typing import MatLike
from typing import ClassVar, Optional, Callable, List, Tuple

from one_dragon.base.geometry.point import Point
from one_dragon.base.geometry.rectangle import Rect
from one_dragon.base.matcher.match_result import MatchResult
from one_dragon.base.operation.operation_base import OperationResult
from one_dragon.base.operation.operation_node import operation_node
from one_dragon.base.operation.operation_round_result import OperationRoundResult
from one_dragon.utils import cal_utils
from one_dragon.utils.i18_utils import gt
from one_dragon.utils.log_utils import log
from sr_od.app.world_patrol.world_patrol_enter_fight import WorldPatrolEnterFight
from sr_od.config.game_config import RunModeEnum
from sr_od.context.sr_context import SrContext
from sr_od.operations.move import cal_pos_utils
from sr_od.operations.move.cal_pos_utils import VerifyPosInfo
from sr_od.operations.move.get_rid_of_stuck import GetRidOfStuck
from sr_od.operations.sr_operation import SrOperation
from sr_od.operations.technique import UseTechnique
from sr_od.screen_state import common_screen_state, battle_screen_state
from sr_od.sr_map import mini_map_utils, large_map_utils
from sr_od.sr_map.large_map_info import LargeMapInfo
from sr_od.sr_map.mini_map_info import MiniMapInfo
from sr_od.sr_map.sr_map_def import Region


class MoveDirectly(SrOperation):

    max_len: int = 6  # 最多存储多少个走过的坐标
    rec_pos_interval: float = 0.5  # 间隔多少秒记录一次坐标
    stuck_distance: float = 20  # 移动距离多少以内认为是被困
    arrival_distance: float = 10  # 多少距离内认为是到达目的地
    fail_after_no_battle: float = 120  # 多少秒无战斗后退出 通常不会有路线这么久都遇不到怪 只能是卡死了 然后脱困算法又让角色产生些位移

    STATUS_NO_POS: ClassVar[str] = '无法识别坐标'

    def __init__(self, ctx: SrContext,
                 lm_info: LargeMapInfo,
                 start: Point,
                 target: Point,
                 next_lm_info: Optional[LargeMapInfo] = None,
                 stop_afterwards: bool = True,
                 no_run: bool = False,
                 no_battle: bool = False,
                 technique_fight: bool = False,
                 technique_only: bool = False,
                 op_callback: Optional[Callable[[OperationResult], None]] = None):
        """
        从当前位置 朝目标点直线前行
        有简单的脱困功能

        1. 最开始使用传入的开始坐标 直接开始移动
        2. 移动途中计算新的坐标，计算坐标会判断在一定距离内，且方向与当前人物朝向基本一致
        3. 由于第2点，移动途中不应该有任何移动外转向，避免计算坐标被舍弃
        4. 移动途中可能被怪物锁定而停下来
        """
        SrOperation.__init__(self, ctx, op_name=gt('移动 %s -> %s') % (start, target), op_callback=op_callback)
        self.lm_info: LargeMapInfo = lm_info
        self.next_lm_info: LargeMapInfo = next_lm_info
        self.region: Region = lm_info.region
        self.target: Point = target
        self.start_pos: Point = start
        self.pos: List[Point] = []
        self.stuck_times = 0  # 被困次数
        self.last_rec_time = 0  # 上一次记录坐标的时间
        self.no_pos_times = 0  # 累计算不到坐标的次数
        self.stop_afterwards = stop_afterwards  # 最后是否停止前进
        self.last_no_pos_time = 0  # 上一次算不到坐标的时间 目前算坐标太快了 可能地图还在缩放中途就已经失败 所以稍微隔点时间再记录算不到坐标
        self.stop_move_time: Optional[float] = None  # 停止移动的时间
        self.last_move_stuck_time: float = 0  # 上一次脱困结束的时间

        self.run_mode = RunModeEnum.OFF.value.value if no_run else self.ctx.game_config.run_mode
        self.no_battle: bool = no_battle  # 本次移动是否保证没有战斗
        self.technique_fight: bool = technique_fight  # 是否使用秘技进入战斗
        self.technique_only: bool = technique_only  # 是否只使用秘技进入战斗

    def handle_init(self):
        """
        执行前的初始化 由子类实现
        注意初始化要全面 方便一个指令重复使用
        可以返回初始化后判断的结果
        - 成功时跳过本指令
        - 失败时立刻返回失败
        - 不返回时正常运行本指令
        """
        now = time.time()
        self.last_rec_time = now - 1
        self.last_battle_time = now  # 上一次在战斗的时间 用于判断是否长时间没有进入战斗 然后退出
        self.last_battle_exit_with_alert: bool = False  # 上一次战斗指令退出时 仍然有告警。说明人物卡住了，后续要先忽略攻击告警进行移动。
        self.pos = []
        if self.ctx.controller.is_moving:  # 连续移动的时候 使用开始点作为一个起始点
            self.pos.append(self.start_pos)
        self.stop_move_time = None

        return None

    @operation_node(name='画面识别', is_start_node=True)
    def check_screen(self) -> OperationRoundResult:
        now_time = time.time()

        if not self.no_battle and now_time - self.last_battle_time > MoveDirectly.fail_after_no_battle:
            return self.round_fail('移动超时')

        stuck = self.move_in_stuck()  # 先尝试脱困 再进行移动
        if stuck is not None:  # 只有脱困失败的情况会返回 round_fail
            return stuck

        screen = self.screenshot()

        if common_screen_state.is_normal_in_world(self.ctx, screen):
            return self.handle_in_world(screen, now_time)
        else:
            return self.handle_not_in_world(screen, now_time)

    def handle_not_in_world(self, screen: MatLike, now_time: float) -> OperationRoundResult:
        """
        不在大世界中 进行处理
        目前就只会进入了战斗
        :return:
        """
        self.ctx.controller.stop_moving_forward()
        self.ctx.last_use_tech_time = 0  # 不在大世界的情况 都认为是秘技生效了 所以重置时间
        log.info('移动中被袭击')
        return self.do_attack(False)

    def get_fight_op(self, in_world: bool = True) -> SrOperation:
        """
        移动过程中被袭击时候处理的指令
        :return:
        """
        if in_world:
            first_state = common_screen_state.ScreenState.NORMAL_IN_WORLD.value
        else:
            first_state = battle_screen_state.ScreenState.BATTLE.value
        return WorldPatrolEnterFight(self.ctx,
                                     technique_fight=self.technique_fight,
                                     technique_only=self.technique_only,
                                     first_state=first_state)

    def handle_in_world(self, screen: MatLike, now_time: float) -> OperationRoundResult:
        """
        在大世界中 进行处理
        :return:
        """
        if self.ctx.world_patrol_fx_should_use_tech:
            # 特殊处理飞霄逻辑 使用秘技
            op = UseTechnique(
                self.ctx,
                max_consumable_cnt=self.ctx.world_patrol_config.max_consumable_cnt,
                need_check_point=True,  # 检查秘技点是否足够 可以在没有或者不能用药的情况加快判断
                trick_snack=self.ctx.game_config.use_quirky_snacks,
                exit_after_use=True
            )
            op.execute()
            return self.round_wait('飞霄使用秘技')

        # 先异步识别是否需要攻击
        if (not self.no_battle  # 如果外层调用保证没有战斗 跳过识别
            and not self.last_battle_exit_with_alert  # 如果上一次的战斗指令是有告警地退出，说明人物卡住了，先移动，不识别攻击
        ):
            submit, attack_future = self.ctx.yolo_detector.detect_should_attack_in_world_async(screen, now_time)
            log.debug('提交攻击检测 %s', submit)

        mm = mini_map_utils.cut_mini_map(screen, self.ctx.game_config.mini_map_pos)

        next_pos, mm_info = self.cal_pos(mm, now_time)  # 计算当前坐标

        check_no_pos = self.check_no_pos(next_pos, now_time)  # 坐标计算失败处理
        if check_no_pos is None:
            # 能识别到坐标的时候 先判断是否到达 就算被怪锁定 也交给下一个patrol指令攻击
            check_arrive = self.check_arrive(next_pos)
            if check_arrive is not None:
                return check_arrive

        # 被敌人锁定的时候 小地图会被染红 坐标匹配能力大减
        # 因此 就算识别不到坐标 也要判断是否被怪锁定 以免一直识别坐标失败站在原地被袭
        check_enemy = self.check_enemy_and_attack(screen, mm_info.raw_del_radio, now_time)
        if check_enemy is not None:
            return check_enemy

        if check_no_pos is not None:
            return check_no_pos

        self.move(next_pos, now_time, mm_info)

        return self.round_wait()

    def move_in_stuck(self) -> Optional[OperationRoundResult]:
        """
        判断是否被困且进行移动
        :return: 如果被困次数过多就返回失败
        """
        if time.time() - self.last_move_stuck_time < 1:  # 上一次尝试脱困后 过一会再尝试下一次脱困
            return None
        if len(self.pos) == 0 or self.no_pos_times > 0:  # 识别不到坐标的时候也不要乱走了
            return None

        first_pos = self.pos[0]
        last_pos = self.pos[len(self.pos) - 1]

        # 通过第一个坐标和最后一个坐标的距离 判断是否困住了
        if len(self.pos) >= MoveDirectly.max_len and \
                cal_utils.distance_between(first_pos, last_pos) < MoveDirectly.stuck_distance:
            self.stuck_times += 1
            if self.stuck_times > 12:
                return self.round_fail('脱困失败')
            get_rid_of_stuck = GetRidOfStuck(self.ctx, self.stuck_times)
            stuck_op_result = get_rid_of_stuck.execute()
            if stuck_op_result.success:
                self.last_rec_time += stuck_op_result.data
            self.last_move_stuck_time = time.time()
        else:
            self.stuck_times = 0

        return None

    def check_enemy_and_attack(self, screen: MatLike, mm: MatLike, now_time: float) -> Optional[OperationRoundResult]:
        """
        从小地图检测敌人 如果有的话 进入索敌
        :param screen: 游戏画面
        :param mm: 小地图部分
        :param now_time: 当前时间
        :return: 是否有敌人
        """
        if self.no_battle:  # 外层调用保证没有战斗 跳过后续检测
            return None
        if not self.ctx.yolo_detector.should_attack_in_world_last_result(now_time):
            return None

        return self.do_attack(True)

    def do_attack(self, in_world: bool) -> OperationRoundResult:
        """
        实施攻击
        :return:
        """
        # 停止移动的指令交给了 WorldPatrolEnterFight 这样可以通过攻击或者十方秘技来取消停止移动造成的后摇
        if self.stop_move_time is None:
            self.stop_move_time = time.time() + (1 if self.run_mode != RunModeEnum.OFF.value.value else 0)

        fight = self.get_fight_op(in_world=in_world)
        fight_start_time = time.time()
        op_result = fight.execute()
        if not op_result.success:
            return self.round_fail(status=op_result.status, data=op_result.data)
        else:
            self.last_battle_exit_with_alert = op_result.status == WorldPatrolEnterFight.STATUS_EXIT_WITH_ALERT

        fight_end_time = time.time()

        self.last_battle_time = fight_end_time
        self.last_rec_time += fight_end_time - fight_start_time  # 战斗可能很久 更改记录时间
        self.ctx.pos_info.pos_first_cal_pos_after_fight = True

        return self.round_wait()

    def cal_pos(self, mm: MatLike, now_time: float) -> Tuple[Optional[Point], MiniMapInfo]:
        """
        根据上一次的坐标和行进距离 计算当前位置坐标
        :param mm: 小地图截图
        :param now_time: 当前时间
        :return:
        """
        # 根据上一次的坐标和行进距离 计算当前位置
        if self.last_rec_time > 0:
            if self.stop_move_time is not None:
                move_time = self.stop_move_time - self.last_rec_time  # 停止移动后的时间不应该纳入计算
            else:
                move_time = now_time - self.last_rec_time
            if move_time < 1:
                move_time = 1
        else:
            move_time = 1
        if self.ctx.pos_info.pos_first_cal_pos_after_fight:
            move_time += 1  # 扩大范围 兼容攻击时产生的位移

        log.debug('上次记录时间 %.2f 停止移动时间 %.2f 当前时间 %.2f',
                  self.last_rec_time,
                  0 if self.stop_move_time is None else self.stop_move_time,
                  now_time)

        move_distance = self.ctx.controller.cal_move_distance_by_time(move_time)
        last_pos = self.pos[len(self.pos) - 1] if len(self.pos) > 0 else self.start_pos
        possible_pos = (last_pos.x, last_pos.y, move_distance)
        log.debug('准备计算人物坐标 使用上一个坐标为 %s 移动时间 %.2f 是否在移动 %s', possible_pos,
                  move_time, self.ctx.controller.is_moving)
        lm_rect = large_map_utils.get_large_map_rect_by_pos(self.lm_info.gray.shape, mm.shape[:2], possible_pos)

        mm_info = mini_map_utils.analyse_mini_map(mm)

        if len(self.pos) == 0:  # 第一个可以直接使用开始点 不进行计算
            return self.start_pos, mm_info

        # 正确移动时 人物不应该偏离直线太远
        # 攻击后 可能因为攻击产生了位移 允许远一点
        # 脱困移动时 会向左右移动 允许远一点
        if self.ctx.pos_info.pos_first_cal_pos_after_fight or self.stuck_times > 0:
            max_line_distance = self.ctx.controller.walk_speed * 2
        else:
            max_line_distance = self.ctx.controller.walk_speed

        verify = VerifyPosInfo(last_pos=last_pos, max_distance=move_distance,
                               line_p1=self.start_pos, line_p2=self.target,
                               max_line_distance=max_line_distance
                               )

        next_pos = self.do_cal_pos(mm_info, lm_rect, verify)

        if next_pos is None:
            log.error('无法判断当前人物坐标')
            if self.ctx.env_config.is_debug and self.no_pos_times == 0:  # 只记录第一次识别坐标失败的
                cal_pos_utils.save_as_test_case_async(mm, self.region, verify)
        else:
            if self.ctx.record_coordinate and now_time - self.last_rec_time > 0.5:
                # RecordCoordinate.save(self.region, mm, next_pos)
                pass
        return next_pos.center if next_pos is not None else None, mm_info

    def do_cal_pos(self, mm_info: MiniMapInfo,
                   lm_rect: Rect, verify: VerifyPosInfo) -> Optional[MatchResult]:
        """
        真正的计算坐标
        :param mm_info: 当前的小地图信息
        :param lm_rect: 使用的大地图范围
        :param verify: 用于验证坐标的信息
        :return:
        """
        try:
            real_move_time = self.ctx.controller.get_move_time()
            next_pos = cal_pos_utils.cal_character_pos(
                self.ctx, self.lm_info, mm_info,
                lm_rect=lm_rect, retry_without_rect=False,
                running=self.ctx.controller.is_moving,
                real_move_time=real_move_time,
                verify=verify)
            if next_pos is None and self.next_lm_info is not None:
                next_pos = cal_pos_utils.cal_character_pos(
                    self.ctx, self.next_lm_info, mm_info,
                    lm_rect=lm_rect, retry_without_rect=False,
                    running=self.ctx.controller.is_moving,
                    real_move_time=real_move_time,
                    verify=verify)
        except Exception:
            next_pos = None
            log.error('识别坐标失败', exc_info=True)

        return next_pos

    def check_no_pos(self, next_pos: Point, now_time: float) -> Optional[OperationRoundResult]:
        """
        并判断是否识别不到坐标
        过长时间无法识别需要停止角色移动 甚至错误退出
        :param next_pos: 计算得到的坐标
        :param now_time: 当前时间
        :return:
        """
        if next_pos is None:
            if now_time - self.last_no_pos_time > 0.5:
                self.ctx.controller.enter_running(False)  # 不疾跑避免跑远了
                self.no_pos_times += 1
                self.last_no_pos_time = now_time
                if self.no_pos_times >= 3:  # 不要再乱走了
                    self.ctx.controller.stop_moving_forward()
                    if self.stop_move_time is None:
                        self.stop_move_time = now_time + 1  # 加1秒代表惯性
                if self.no_pos_times >= 10:
                    return self.round_fail(MoveDirectly.STATUS_NO_POS)
            return self.round_wait()
        else:
            self.no_pos_times = 0
            self.ctx.pos_info.pos_first_cal_pos_after_fight = False

    def check_arrive(self, next_pos: Point) -> Optional[OperationRoundResult]:
        """
        检查是否已经到达目标点
        :param next_pos:
        :return:
        """
        if cal_utils.distance_between(next_pos, self.target) < MoveDirectly.arrival_distance:
            if self.stop_afterwards:
                self.ctx.controller.stop_moving_forward()
            self.ctx.pos_info.update_pos_after_move(next_pos, region=None if self.next_lm_info is None else self.next_lm_info.region)
            return self.round_success(data=next_pos)
        return None

    def move(self, next_pos: Point, now_time: float, mm_info: MiniMapInfo):
        """
        移动
        :param next_pos:
        :param now_time:
        :param mm_info:
        :return:
        """
        self.stop_move_time = None
        self.ctx.pos_info.update_pos_after_move(next_pos)
        if now_time - self.last_rec_time > self.rec_pos_interval:  # 隔一段时间才记录一个点
            self.ctx.controller.move_towards(next_pos, self.target, mm_info.angle,
                                             run=self.run_mode == RunModeEnum.BTN.value.value)
            # time.sleep(0.5)  # 如果使用小箭头计算方向 则需要等待人物转过来再进行下一轮
            self.pos.append(next_pos)
            log.debug('记录坐标 %s', next_pos)
            if len(self.pos) > MoveDirectly.max_len:
                del self.pos[0]
            self.last_rec_time = now_time

    def handle_pause(self, e=None):
        """
        暂停后的处理 由子类实现
        :return:
        """
        self.ctx.controller.stop_moving_forward()

    def handle_resume(self) -> None:
        """
        恢复运行后的处理 由子类实现
        :return:
        """
        self.last_rec_time += self.current_pause_time
        self.last_battle_time += self.current_pause_time

    def after_operation_done(self, result: OperationResult):
        SrOperation.after_operation_done(self, result)
        if not result.success:
            self.ctx.controller.stop_moving_forward()