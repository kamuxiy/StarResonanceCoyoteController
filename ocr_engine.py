import pytesseract
from PIL import Image
import re
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List, Tuple


@dataclass
class PlayerHealth:
    name: str = ""
    uid: int = 0
    profession: str = ""
    health_percent: float = 0.0
    current_hp: int = 0
    max_hp: int = 0
    is_self: bool = False


@dataclass
class GameState:
    current_pulse: int = 0
    next_bonus: int = 0
    trigger_count: int = 0
    one_click_bonus: int = 0
    bonus_condition: str = ""
    has_team_list: bool = False
    players: List[PlayerHealth] = field(default_factory=list)
    self_health: PlayerHealth = field(default_factory=PlayerHealth)


class OCREngine:
    def __init__(self, tesseract_cmd=None, lang='chi_sim+eng'):
        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        self.lang = lang
        self.last_state = GameState()

    def extract_text(self, image: Image.Image) -> str:
        try:
            text = pytesseract.image_to_string(image, lang=self.lang)
            return text
        except Exception as e:
            print(f"OCR error: {e}")
            return ""

    def extract_digits(self, image: Image.Image) -> str:
        try:
            text = pytesseract.image_to_string(image, config='--psm 7 -c tessedit_char_whitelist=0123456789/')
            return text.strip()
        except Exception as e:
            print(f"OCR digits error: {e}")
            return ""

    def parse_game_state(self, text: str) -> GameState:
        state = GameState()

        patterns = {
            'current_pulse': [
                r'当前脉冲强度[：:]\s*(\d+)',
                r'脉冲强度[：:]\s*(\d+)',
                r'当前脉冲[：:]\s*(\d+)',
            ],
            'next_bonus': [
                r'下次触发时强度加成[：:]\s*(\d+)',
                r'下次加成[：:]\s*(\d+)',
                r'下次触发加成[：:]\s*(\d+)',
            ],
            'trigger_count': [
                r'已触发加成[：:]\s*(\d+)\s*次',
                r'已触发\s*(\d+)\s*次',
                r'触发次数[：:]\s*(\d+)',
            ],
            'one_click_bonus': [
                r'一键点火强度加成[：:]\s*(\d+)',
                r'一键点火加成[：:]\s*(\d+)',
                r'点火加成[：:]\s*(\d+)',
            ],
            'bonus_condition': [
                r'强度加成条件[：:]\s*(.+)',
                r'加成条件[：:]\s*(.+)',
                r'触发条件[：:]\s*(.+)',
            ],
        }

        for field, regex_list in patterns.items():
            for pattern in regex_list:
                match = re.search(pattern, text)
                if match:
                    value = match.group(1).strip()
                    if field == 'bonus_condition':
                        setattr(state, field, value)
                    else:
                        try:
                            setattr(state, field, int(value))
                        except ValueError:
                            pass
                    break

        return state

    def analyze_health_bar(self, image: Image.Image) -> Tuple[float, int, int]:
        if image is None:
            return 0.0, 0, 0

        try:
            img_array = np.array(image)
            if len(img_array.shape) != 3:
                return 0.0, 0, 0

            h, w, _ = img_array.shape
            if h == 0 or w == 0:
                return 0.0, 0, 0

            r_channel = img_array[:, :, 0].astype(int)
            g_channel = img_array[:, :, 1].astype(int)
            b_channel = img_array[:, :, 2].astype(int)
            brightness = r_channel + g_channel + b_channel

            gold_mask = (
                (r_channel > 80) &
                (g_channel > 60) &
                (r_channel > b_channel * 1.2) &
                (g_channel > b_channel * 1.1)
            )

            row_gold_counts = np.sum(gold_mask, axis=1)
            if np.max(row_gold_counts) < 5:
                return 0.0, 0, 0

            best_row = int(np.argmax(row_gold_counts))

            gold_rows = np.where(row_gold_counts > np.max(row_gold_counts) * 0.3)[0]
            if len(gold_rows) == 0:
                return 0.0, 0, 0
            bar_top = gold_rows[0]
            bar_bottom = gold_rows[-1]

            combined_gold = np.zeros(w, dtype=bool)
            for y in range(bar_top, bar_bottom + 1):
                combined_gold |= gold_mask[y, :]

            gold_indices = np.where(combined_gold)[0]
            if len(gold_indices) == 0:
                return 0.0, 0, 0

            gold_left = gold_indices[0]
            gold_right = gold_indices[-1]

            row_brightness = brightness[best_row, :]

            bar_left = 0
            search_left_start = max(0, gold_left - int(w * 0.15))
            ref_left_brightness = np.mean(row_brightness[max(0, gold_left-10):gold_left+1])
            threshold_left = ref_left_brightness * 0.5
            for x in range(gold_left, search_left_start, -1):
                if row_brightness[x] < threshold_left:
                    bar_left = x + 1
                    break
            else:
                bar_left = search_left_start

            bar_right = w - 1
            search_right_end = min(w, gold_right + int(w * 0.15))
            ref_right_brightness = np.mean(row_brightness[gold_right:min(w-1, gold_right+10)+1])
            threshold_right = ref_right_brightness * 0.5
            for x in range(gold_right, search_right_end):
                if row_brightness[x] < threshold_right:
                    bar_right = x - 1
                    break
            else:
                bar_right = search_right_end - 1

            total_width = bar_right - bar_left + 1
            if total_width <= 0:
                return 0.0, 0, 0

            percent = (gold_right - bar_left + 1) / total_width * 100.0
            percent = max(0.0, min(100.0, percent))

            return percent, 0, 0

        except Exception as e:
            print(f"血条分析错误: {e}")
            import traceback
            traceback.print_exc()
            return 0.0, 0, 0

    def parse_self_health_from_region(self, health_image: Image.Image) -> PlayerHealth:
        health = PlayerHealth()
        health.is_self = True

        if health_image is None:
            return health

        try:
            percent, _, _ = self.analyze_health_bar(health_image)
            health.health_percent = percent

            text = self.extract_text(health_image)

            hp_match = re.search(r'(\d{3,6})\s*/\s*(\d{3,6})', text)
            if hp_match:
                try:
                    current = int(hp_match.group(1))
                    max_hp = int(hp_match.group(2))
                    health.current_hp = current
                    health.max_hp = max_hp
                    if max_hp > 0:
                        calculated_percent = (current / max_hp) * 100.0
                        if 0 <= calculated_percent <= 100:
                            health.health_percent = calculated_percent
                except ValueError:
                    pass

            if not health.current_hp:
                digits_text = self.extract_digits(health_image)
                hp_match = re.search(r'(\d+)\s*/\s*(\d+)', digits_text)
                if hp_match:
                    try:
                        current = int(hp_match.group(1))
                        max_hp = int(hp_match.group(2))
                        if max_hp > 0 and current <= max_hp:
                            health.current_hp = current
                            health.max_hp = max_hp
                            calculated_percent = (current / max_hp) * 100.0
                            if 0 <= calculated_percent <= 100:
                                health.health_percent = calculated_percent
                    except ValueError:
                        pass

        except Exception as e:
            print(f"解析自身血量错误: {e}")
            import traceback
            traceback.print_exc()

        return health

    def parse_player_name_from_region(self, name_image: Image.Image) -> str:
        if name_image is None:
            return ""

        try:
            text = self.extract_text(name_image)
            lines = text.strip().split('\n')
            for line in lines:
                line = line.strip()
                if line and len(line) >= 2 and len(line) <= 20:
                    line = re.sub(r'[^\w\u4e00-\u9fff]', '', line)
                    if line:
                        return line
        except Exception as e:
            print(f"解析玩家名称错误: {e}")

        return ""

    def parse_team_from_region(self, team_image: Image.Image) -> Tuple[List[PlayerHealth], bool]:
        players = []
        has_team = False

        if team_image is None:
            return players, has_team

        try:
            text = self.extract_text(team_image)
            lines = text.strip().split('\n')

            img_array = np.array(team_image)
            h, w, _ = img_array.shape

            name_pattern = re.compile(r'^[\w\u4e00-\u9fff]{2,20}$')
            current_name = None

            for line in lines:
                line = line.strip()
                if not line:
                    continue

                clean_line = re.sub(r'[^\w\u4e00-\u9fff]', '', line)
                if name_pattern.match(clean_line) and len(clean_line) >= 2:
                    if current_name is None:
                        current_name = clean_line
                        has_team = True
                    continue

                hp_match = re.search(r'(\d+)\s*/\s*(\d+)', line)
                if hp_match and current_name:
                    try:
                        current = int(hp_match.group(1))
                        max_hp = int(hp_match.group(2))
                        percent = (current / max_hp) * 100.0 if max_hp > 0 else 0.0
                        players.append(PlayerHealth(
                            name=current_name,
                            health_percent=percent,
                            current_hp=current,
                            max_hp=max_hp,
                            is_self=False
                        ))
                        current_name = None
                    except ValueError:
                        pass
                    continue

                percent_match = re.search(r'(\d+(?:\.\d+)?)\s*%', line)
                if percent_match and current_name:
                    try:
                        percent = float(percent_match.group(1))
                        players.append(PlayerHealth(
                            name=current_name,
                            health_percent=percent,
                            is_self=False
                        ))
                        current_name = None
                    except ValueError:
                        pass
                    continue

            if has_team and not players:
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    match = re.search(r'([\w\u4e00-\u9fff]{2,20}).*?(\d+)\s*/\s*(\d+)', line)
                    if match:
                        try:
                            name = match.group(1)
                            current = int(match.group(2))
                            max_hp = int(match.group(3))
                            percent = (current / max_hp) * 100.0 if max_hp > 0 else 0.0
                            players.append(PlayerHealth(
                                name=name,
                                health_percent=percent,
                                current_hp=current,
                                max_hp=max_hp,
                                is_self=False
                            ))
                            has_team = True
                        except ValueError:
                            pass

        except Exception as e:
            print(f"解析队伍列表错误: {e}")

        return players, has_team

    def detect_state_change(self, new_state: GameState) -> dict:
        changes = {}

        if new_state.current_pulse != self.last_state.current_pulse:
            changes['current_pulse'] = {
                'old': self.last_state.current_pulse,
                'new': new_state.current_pulse
            }

        if new_state.next_bonus != self.last_state.next_bonus:
            changes['next_bonus'] = {
                'old': self.last_state.next_bonus,
                'new': new_state.next_bonus
            }

        if new_state.trigger_count != self.last_state.trigger_count:
            changes['trigger_count'] = {
                'old': self.last_state.trigger_count,
                'new': new_state.trigger_count
            }

        if new_state.one_click_bonus != self.last_state.one_click_bonus:
            changes['one_click_bonus'] = {
                'old': self.last_state.one_click_bonus,
                'new': new_state.one_click_bonus
            }

        if new_state.bonus_condition != self.last_state.bonus_condition:
            changes['bonus_condition'] = {
                'old': self.last_state.bonus_condition,
                'new': new_state.bonus_condition
            }

        old_players = [(p.name, p.health_percent) for p in self.last_state.players]
        new_players = [(p.name, p.health_percent) for p in new_state.players]
        if old_players != new_players:
            changes['players'] = {
                'old': self.last_state.players,
                'new': new_state.players
            }

        if (new_state.self_health.name != self.last_state.self_health.name or
                new_state.self_health.health_percent != self.last_state.self_health.health_percent):
            changes['self_health'] = {
                'old': self.last_state.self_health,
                'new': new_state.self_health
            }

        self.last_state = new_state
        return changes

    def process_image(self, image: Image.Image) -> tuple[GameState, dict]:
        text = self.extract_text(image)
        state = self.parse_game_state(text)
        changes = self.detect_state_change(state)
        return state, changes
