"""COS 业务级操作（基于 scripts/_cos.py 的原子 API 封装）

业务级操作清单：
  - relocate(src_key, dst_key)
        从 _inbox/ 复制文件到目标"考账发工"路径
        （保留 _inbox/ 副本，由 COS 生命周期规则定期清理）

  - resolve_target_path(layer, ent_short, proj_short_or_name, kind, filename)
        生成目标 COS key 路径，符合"考账发工"目录设计

  - update_raw_files_key(cur, raw_file_id, new_key)
        归位成功后回写 raw_files.ai_cos_key

🚨 硬规则（feedback_cos_write_only_ai_bucket）：写操作只允许 ai-1306031257
"""
import sys
sys.path.insert(0, 'D:/小鱼AI数据')
from scripts._cos import client as cos_client, BUCKET as AI_BUCKET, exists


# ============================================================
# 路径生成（统一规范）
# ============================================================
def resolve_target_path(layer, *, ent_short=None, proj_short=None,
                        controller_name=None, kind='考账发工', filename=''):
    """生成 COS 归位目标 key

    layer ∈ {'project', 'enterprise', 'controller'}
        project    → 企业/{ent_short}/项目/{proj_short}/{kind}/{filename}
        enterprise → 企业/{ent_short}/{kind}/{filename}
        controller → 实控人/{controller_name}/{kind}/{filename}

    kind 默认 '考账发工'（4 类核心数据合并术语）；其他可选 '征信' '发票' 等
    """
    if not filename:
        raise ValueError('filename required')
    if layer == 'project':
        if not ent_short or not proj_short:
            raise ValueError('project layer needs ent_short + proj_short')
        return f'企业/{ent_short}/项目/{proj_short}/{kind}/{filename}'
    if layer == 'enterprise':
        if not ent_short:
            raise ValueError('enterprise layer needs ent_short')
        return f'企业/{ent_short}/{kind}/{filename}'
    if layer == 'controller':
        if not controller_name:
            raise ValueError('controller layer needs controller_name')
        return f'实控人/{controller_name}/{kind}/{filename}'
    raise ValueError(f'unknown layer: {layer}')


# ============================================================
# 复制 + 保留备份
# ============================================================
def relocate(src_key, dst_key, overwrite=False):
    """从 src_key 复制到 dst_key（同桶 ai-1306031257 内）

    保留 src 副本（_inbox/ 由 COS 生命周期规则清理）
    overwrite=False 时若 dst 已存在则跳过（idempotent）
    返回 'copied' / 'exists' / 'skipped_same'
    """
    if src_key == dst_key:
        return 'skipped_same'
    if not overwrite and exists(dst_key):
        return 'exists'
    # 同桶 copy
    copy_source = {'Bucket': AI_BUCKET, 'Key': src_key, 'Region': cos_client._conf._region}
    cos_client.copy_object(Bucket=AI_BUCKET, Key=dst_key, CopySource=copy_source)
    return 'copied'


# ============================================================
# raw_files.ai_cos_key 回写
# ============================================================
def update_raw_files_key(cur, raw_file_id, new_key):
    """归位成功后更新 raw_files.ai_cos_key"""
    cur.execute("UPDATE raw_files SET ai_cos_key=%s WHERE id=%s",
                (new_key, raw_file_id))
