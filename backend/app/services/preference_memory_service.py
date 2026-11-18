"""仅记录显式偏好，使用确定性字段槽位，并支持城市覆盖和过期时间。"""
import time
import unicodedata
from typing import Literal
from pydantic import BaseModel, Field, model_validator

CATEGORIES = {
    'pace': {'relaxed': '轻松慢游', 'balanced': '节奏适中', 'intensive': '充实打卡'},
    'schedule': {'early': '愿意早出发', 'late': '不安排早出发', 'flexible': '出发时间灵活'},
    'hotel': {'economy': '经济型住宿', 'comfort': '舒适型住宿', 'luxury': '高档住宿', 'homestay': '民宿'},
    'transport': {'walking': '优先步行', 'transit': '优先公共交通', 'taxi': '优先打车', 'self_drive': '自驾'},
}


def normalize(value):
    return ''.join(unicodedata.normalize('NFKC', value).split()).casefold()


def normalize_city(city):
    return city.strip().removesuffix('市')


class MemoryWrite(BaseModel):
    revision: int = Field(default=0, ge=0)
    content: str = Field(default='', max_length=400)
    city: str = Field(default='', max_length=50)
    enabled: bool = True
    category: Literal['general', 'pace', 'schedule', 'hotel', 'transport', 'interest', 'diet'] = 'general'
    value: str = Field(default='', max_length=100)
    polarity: Literal['prefer', 'avoid'] = 'prefer'
    expires_at: int | None = Field(default=None, ge=0)
    replace_conflicts: bool = False
    conflict_ids: list[str] = Field(default_factory=list, max_length=100)
    conflict_revisions: dict[str, int] = Field(default_factory=dict, max_length=100)

    @model_validator(mode='after')
    def validate_preference(self):
        # 保留显式赋值字段集合，避免旧客户端的局部写入清空新增列。
        for name in ('content', 'value', 'city'):
            object.__setattr__(self, name, getattr(self, name).strip())
        if self.category == 'general' and not self.content:
            raise ValueError('请输入偏好内容')
        if self.category in CATEGORIES:
            if self.value not in CATEGORIES[self.category] or self.polarity != 'prefer':
                raise ValueError('请选择该分类支持的偏好值')
        elif self.category != 'general' and not self.value:
            raise ValueError('请输入偏好对象')
        if self.enabled and self.expires_at is not None and self.expires_at <= time.time():
            raise ValueError('有效期已经结束，请选择未来日期或暂停这条偏好')
        return self


def active(row):
    return bool(row['enabled']) and (row.get('expires_at') is None or row['expires_at'] > time.time())


def slot(row):
    category = row.get('category', 'general')
    return (category, '' if category in CATEGORIES else normalize(row.get('value') or row['content']))


def conflicts(rows, target, memory_id):
    if not active(target):
        return []
    return [r for r in rows if r['id'] != memory_id and active(r)
            and normalize_city(r['city']) == normalize_city(target['city']) and slot(r) == slot(target)]


def effective_memories(rows, city):
    valid = [r for r in rows if active(r) and (not r['city'] or normalize_city(r['city']) == normalize_city(city))]
    valid.sort(key=lambda r: (bool(r['city']), r['updated_at'], r['id']), reverse=True)
    selected, seen = [], set()
    for row in valid:
        key = slot(row)
        if key not in seen:
            selected.append(row)
            seen.add(key)
    return selected


def render_memory(row):
    category = row.get('category', 'general')
    if category == 'general':
        return row['content']
    value = row.get('value', '')
    if category in CATEGORIES:
        return CATEGORIES[category].get(value, row['content'])
    return ('避免' if row.get('polarity') == 'avoid' else '偏好') + value
