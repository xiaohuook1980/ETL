"""parsers：纯解析+写 mart，不识别、不操作 COS。

每个 parser 模块导出统一接口：
    process_sheet(cur, *, project_id, enterprise_id, business_cycle,
                  source_file_id, sheet_name, ws) -> dict
        返回 {'inserted': N, 'skipped': M, ...}

dispatcher 按 sheet kind 路由调用。每个 parser 也支持 CLI 独立调试。
"""
