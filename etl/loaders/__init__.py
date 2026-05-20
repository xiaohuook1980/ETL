"""跨文件批量装载层（mart 多源去重+批量写）

对应方案 B：dispatcher 先把所有文件的 rows 收集到内存，
按 (project_id, kind, natural_key) 去重（晚到 source_file_id 胜出），最后批量 INSERT。

当前仅实现 attendance；bill/payroll/wage_sheet 视情况扩展。
"""
