-- ===================================================================
-- fish-test 基础数据库初始化脚本（v2.2，2026-05-05）
-- ===================================================================
-- 用途：劳务风控 ELT 架构的基础数据库 schema
-- 24 张表（事件溯源版）：raw_files 单表 + raw_mini_shift 镜像
-- 设计文档：参考会话讨论 + .skill-docs/数据分析/规则1+规则2
--
-- v2.0 变更（2026-05-01）：
--   1. raw_jiafang_attendance/bill/wage + raw_bank_daifa 4 表 → raw_files 单表（按 file_hash 文件级去重）
--   2. raw_mini_a_bill / raw_mini_user_shift_rel 主键改 (id) 单列，etl_batch_id 仅作"最后刷新批次"标记
--   3. mart 4 表加 source_file_id 列关联 raw_files.id（事件溯源）
--   4. 异步 worker 程序读 raw_files.parse_status='pending' 解析投影到 mart 表
--
-- v2.1 变更（2026-05-03）：
--   1. mart payrolls 加 UNIQUE KEY (project_id, name_raw, pay_time, work_amount)
--      DB 流和 xlsx 流可同时写入，按三元组（name+时间+金额）去重，先写者占位
--      INSERT IGNORE 跳过冲突，符合 feedback_payroll_dedup
--
-- v2.2 变更（2026-05-05，项目注册功能）：
--   1. projects 加 daishou_threshold INT DEFAULT 2000（代收阈值）
--   2. projects 加 source_created_at DATETIME（老库项目创建时间，增量同步过滤）
--   3. 新建 project_registrations 临时表（DB 合并后可废弃）
--
-- v2.3 变更（2026-05-06，项目归属规则）：
--   1. 新建 project_attribution_rules 表（项目级 sheet/column 归属规则）
--   2. 解析时遍历同企业下所有项目规则做归属，替代 sheet_route_keywords/payroll_filter/kaoqin_filter 三个分散字段
--
-- 用法：
--   mysql -h ... -u prod_fish_test -p fish-test < init_fish_test.sql
-- 或：source D:/小鱼AI数据/etl/sql/init_fish_test.sql;
--
-- ⚠️ 该脚本会 DROP 所有相关表，请在 fish-test 库（非 fish-prod）执行
-- ===================================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;
SET SQL_MODE = 'NO_AUTO_VALUE_ON_ZERO';

-- ===================================================================
-- 反向 DROP（按依赖反序，子表先删）
-- ===================================================================
DROP TABLE IF EXISTS coverage_summary_by_controller;
DROP TABLE IF EXISTS coverage_offset_records;
DROP TABLE IF EXISTS coverage_excess_detail;
DROP TABLE IF EXISTS return_records;
DROP TABLE IF EXISTS return_time_config;
DROP TABLE IF EXISTS loan_records;
DROP TABLE IF EXISTS temp_occupations;
DROP TABLE IF EXISTS credit_limits;
DROP TABLE IF EXISTS controller_enterprise_map;
DROP TABLE IF EXISTS controllers;
DROP TABLE IF EXISTS wage_sheets;
DROP TABLE IF EXISTS payrolls;
DROP TABLE IF EXISTS bill_persons;
DROP TABLE IF EXISTS bill_totals;
DROP TABLE IF EXISTS bills;
DROP TABLE IF EXISTS attendance_summary;
DROP TABLE IF EXISTS attendance;
DROP TABLE IF EXISTS raw_files;
DROP TABLE IF EXISTS raw_mini_shift;
DROP TABLE IF EXISTS raw_mini_a_bill;
DROP TABLE IF EXISTS raw_mini_user_shift_rel;
-- v1.0 弃用表（2026-04-30 设计，2026-05-01 改为 raw_files 单表后清理）
DROP TABLE IF EXISTS raw_bank_daifa;
DROP TABLE IF EXISTS raw_jiafang_wage;
DROP TABLE IF EXISTS raw_jiafang_bill;
DROP TABLE IF EXISTS raw_jiafang_attendance;
DROP TABLE IF EXISTS project_attribution_rules;
DROP TABLE IF EXISTS unit_prices;
DROP TABLE IF EXISTS business_cycles;
DROP TABLE IF EXISTS project_registrations;
DROP TABLE IF EXISTS projects;
DROP TABLE IF EXISTS enterprises;
DROP TABLE IF EXISTS workers;
DROP TABLE IF EXISTS etl_batches;

-- ===================================================================
-- 1. 元数据：etl_batches
-- ===================================================================
CREATE TABLE etl_batches (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  started_at DATETIME NOT NULL                         COMMENT '批次开始时间',
  finished_at DATETIME                                 COMMENT '完成时间，运行中为 NULL',
  scope_enterprise VARCHAR(64)                         COMMENT '企业简称（如"希锐"），NULL=全局',
  scope_project VARCHAR(64)                            COMMENT '项目（如"丽盈普工"），NULL=企业级',
  modules JSON                                         COMMENT '本次跑的模块 ["attendance","bills"]',
  triggered_by VARCHAR(32)                             COMMENT 'web / cli / cron / api',
  triggered_user VARCHAR(64),
  uploaded_files JSON                                  COMMENT '[{path,size,sha256}]',
  status VARCHAR(16) NOT NULL DEFAULT 'running'        COMMENT 'running/ok/failed/rolled_back',
  error_message TEXT,
  raw_rows JSON                                        COMMENT '{"raw_xxx": N}',
  mart_rows JSON                                       COMMENT '{"attendance": N}',
  assertion_results JSON,
  KEY idx_scope_time (scope_enterprise, scope_project, started_at),
  KEY idx_status (status, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ETL 批次记录';

-- ===================================================================
-- 2. 字典：workers（员工）
-- ===================================================================
CREATE TABLE workers (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  id_card_clean VARCHAR(18)                            COMMENT '完整身份证（去脱敏星号）',
  name VARCHAR(64) NOT NULL,
  mobile VARCHAR(20),
  binding_project_id BIGINT                            COMMENT '档4(仅姓名)绑定项目；其他档为 NULL',
  id_source VARCHAR(32)                                COMMENT 'full_id / desensitized / name_mobile / name_only',
  duplicate_flag TINYINT NOT NULL DEFAULT 0            COMMENT '0正常 1同名异人/脱敏歧义/手动标记',
  first_seen_at DATETIME,
  last_seen_at DATETIME,
  note VARCHAR(255),
  UNIQUE KEY uk_id_card (id_card_clean),
  UNIQUE KEY uk_name_proj (name, binding_project_id),
  KEY idx_name_mobile (name, mobile)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='员工字典（不含实控人/担保人）';

-- ===================================================================
-- 3. 配置：enterprises（企业）
-- ===================================================================
CREATE TABLE enterprises (
  id BIGINT NOT NULL PRIMARY KEY                       COMMENT '直接用 fish-prod.biz_enterprise.id',
  full_name VARCHAR(255) NOT NULL                      COMMENT '工商全称',
  short_name VARCHAR(64) NOT NULL                      COMMENT '简称（暗语用，如"希锐"）',
  unified_credit_code VARCHAR(32),
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  note VARCHAR(255),
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_short (short_name),
  KEY idx_full (full_name),
  KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='企业字典';

-- ===================================================================
-- 4. 配置：projects（项目 + 项目信息）
-- ===================================================================
CREATE TABLE projects (
  id BIGINT NOT NULL PRIMARY KEY                       COMMENT 'fish-prod.mini_project.id 或 mini_pre_project.project_id',
  pre_id BIGINT                                        COMMENT 'mini_pre_project.id，可空',
  source_created_at DATETIME                           COMMENT '老库 mini_project.created_at；增量同步过滤用',
  enterprise_id BIGINT NOT NULL,
  title VARCHAR(128) NOT NULL                          COMMENT '系统侧 project_title（如"丽盈普工"）',
  short_name VARCHAR(64)                               COMMENT '别名/简称（用户暗语）',
  jiafang_name VARCHAR(255)                            COMMENT '甲方名称',
  finance_mode VARCHAR(32) NOT NULL DEFAULT 'normal'   COMMENT 'normal / prepay / weekly_prepay',
  business_cycle VARCHAR(32) NOT NULL DEFAULT '自然月' COMMENT '"自然月" / "上月26-本月25"',
  payroll_cycle VARCHAR(64),
  profit_ratio DECIMAL(4,3) NOT NULL DEFAULT 0.800,
  daishou_threshold INT NOT NULL DEFAULT 2000          COMMENT '代收阈值（元），出款计算用',
  jiafang_contract_period VARCHAR(128),
  baoli_contract_period VARCHAR(128),
  insurance_compliance VARCHAR(255),
  payroll_filter_keywords JSON                         COMMENT 'xlsx 发薪流水按备注含任一关键词过滤本项目；NULL=不过滤（task #6 P1）',
  kaoqin_filter_keywords JSON                          COMMENT '考勤 xlsx 按劳务公司列含任一关键词过滤本项目；NULL=不过滤（task #6 P1）',
  sheet_route_keywords JSON                            COMMENT '同 enterprise 下多 sheet 一文件混合多项目时，sheet name 匹配本项目的关键词（如梦寺达"长隆融资.xlsx"按 sheet 名"企鹅1/横琴1/飞船酒店"路由）；NULL=不参与路由',
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  note VARCHAR(255),
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_short (enterprise_id, short_name),
  KEY idx_ent (enterprise_id, status),
  KEY idx_title (title)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='项目字典 + 项目信息基本配置';

-- ===================================================================
-- 4b. 配置：business_cycles（项目业务周期，多版本支持历史变更）
-- ===================================================================
-- 设计：
--   - cycle_type='自然月' → 1 号到月底
--   - cycle_type='非自然月' + start_day=26 → 上月 26 日 ~ 本月 25 日（如澳思美）
--   - effective_start/end 处理项目周期变更（合同重签等）
--   - projects.business_cycle 字段保留作冗余（兼容旧代码）
CREATE TABLE business_cycles (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  project_id BIGINT NOT NULL,
  cycle_type VARCHAR(16) NOT NULL                      COMMENT '"自然月" / "非自然月"',
  start_day TINYINT NOT NULL DEFAULT 1                 COMMENT '开始日（1-31）；自然月固定=1；非自然月如 26',
  effective_start DATE                                 COMMENT '生效起始（NULL=自创建起）',
  effective_end DATE                                   COMMENT '生效终止（NULL=持续）',
  note VARCHAR(255),
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_proj_eff (project_id, effective_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='项目业务周期配置（多版本）';

-- ===================================================================
-- 4c. 配置：project_registrations（Web 注册状态，临时表）
-- ===================================================================
-- 设计：
--   - 同步进来的项目默认 'unregistered'，注册后 'active'，注销 'disabled'
--   - 不掺合 projects.status（项目本身的生命周期），独立存
--   - fish-test/fish-prod 合并后此表废弃（DROP 即可）
CREATE TABLE project_registrations (
  project_id BIGINT NOT NULL PRIMARY KEY,
  status VARCHAR(16) NOT NULL DEFAULT 'unregistered'
    COMMENT 'unregistered / registered / disabled',
  registered_at DATETIME,
  disabled_at DATETIME,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Web 注册状态（临时表，DB 合并后废弃）';

-- ===================================================================
-- 4d. 配置：project_attribution_rules（项目归属规则）
-- ===================================================================
-- 解析考账发工文件时，按本表把 sheet/行装到对应项目。
-- 每项目每类（kaoqin_bill/wage/payroll）每种规则（sheet/column）一行，最多 6 行/项目。
-- - sheet 规则：keywords 命中 sheet 名 → 该 sheet 整页归本项目
-- - column 规则：column_names 任一命中表头 → 该列每行值含 keywords → 该行归本项目
CREATE TABLE project_attribution_rules (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  project_id BIGINT NOT NULL,
  category VARCHAR(16) NOT NULL                         COMMENT 'kaoqin_bill / wage / payroll',
  scope VARCHAR(16) NOT NULL DEFAULT 'project'          COMMENT 'enterprise=企业过滤（仅 kaoqin_bill 用）；project=项目过滤',
  rule_type VARCHAR(8) NOT NULL                         COMMENT 'sheet / column',
  column_names VARCHAR(255) NOT NULL DEFAULT ''         COMMENT 'column 类型时填一个列名；sheet 类型为空',
  keywords JSON NOT NULL                                COMMENT '关键词数组，任一命中即匹配',
  enabled TINYINT NOT NULL DEFAULT 0                    COMMENT '0=禁用（按系统挂载位置归属）；1=启用此规则参与关键词匹配',
  match_count INT NOT NULL DEFAULT 0,
  last_matched_at DATETIME,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_proj_cat_scope_type_col (project_id, category, scope, rule_type, column_names),
  KEY idx_category (category, scope, rule_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='项目归属规则（按本项目规则过滤本项目数据；考账双层 enterprise+project，工资/发薪仅 project）';

-- ===================================================================
-- 4e. 配置：project_classify_rules（项目级 sheet 分类规则，2026-05-09 新增）
-- ===================================================================
-- 替代旧 etl/classify.py 关键词字典：每项目独立配置 sheet kind 识别规则。
-- handler='standard' 走配置的 column_mapping；其他 handler 走 etl/parsers/handlers/<name>.py
-- match_columns 支持 * 单字符通配（"*月平日工时" / "**月平日工时"）
DROP TABLE IF EXISTS project_classify_rules;
CREATE TABLE project_classify_rules (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  project_id BIGINT NOT NULL,
  target_kind VARCHAR(16) NOT NULL                      COMMENT 'attendance / bill / wage_sheet / payroll',
  priority SMALLINT NOT NULL DEFAULT 100                COMMENT '同 kind 内尝试顺序（小→大）',
  match_columns JSON NOT NULL                           COMMENT 'AND 必含列名数组，支持 * 通配',
  match_columns_any JSON                                COMMENT 'OR 至少含一项（可空）',
  match_excludes JSON                                   COMMENT 'NOT 任一命中即不算（可空）',
  scan_rows TINYINT NOT NULL DEFAULT 4                  COMMENT '扫表头扫前几行（4 / 10）',
  handler VARCHAR(64) NOT NULL DEFAULT 'standard'       COMMENT 'standard 走 column_mapping；其他走 handlers/<name>.py',
  column_mapping JSON                                   COMMENT 'handler=standard 时填 {"姓名":"name_raw",...}',
  enabled TINYINT NOT NULL DEFAULT 1,
  match_count INT NOT NULL DEFAULT 0,
  last_matched_at DATETIME,
  note VARCHAR(255),
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_proj_kind_priority (project_id, target_kind, priority, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='项目级 sheet 分类规则（特征列识别 + handler 路由）';

-- ===================================================================
-- 4f. 配置：pending_classify_sheets（待识别 sheet 待办，2026-05-09 新增）
-- ===================================================================
-- dispatcher 解析时遇到 unknown sheet 入这里 → UI 上展示供用户一键加规则
DROP TABLE IF EXISTS pending_classify_sheets;
CREATE TABLE pending_classify_sheets (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  project_id BIGINT NOT NULL,
  raw_file_id BIGINT NOT NULL,
  sheet_name VARCHAR(255) NOT NULL,
  headers_preview TEXT NOT NULL                         COMMENT '前 4 行表头拼接，给用户看着配规则',
  status VARCHAR(16) NOT NULL DEFAULT 'pending'         COMMENT 'pending / resolved / ignored',
  first_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resolved_at DATETIME,
  resolved_rule_id BIGINT,
  UNIQUE KEY uk_proj_file_sheet (project_id, raw_file_id, sheet_name),
  KEY idx_status (project_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='待识别 sheet 待办（unknown 入这里 → UI 一键加规则）';

-- ===================================================================
-- 4g. 配置：project_validity_rules（数据有效性规则，2026-05-09 新增）
-- ===================================================================
-- 行级过滤：每条规则独立判断，命中 → mart 行 is_valid=0 + invalid_reason=note
-- 含系统内置规则（合计行剔除等，is_builtin=1，UI 上不可删但可扩展关键词）
DROP TABLE IF EXISTS project_validity_rules;
CREATE TABLE project_validity_rules (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  project_id BIGINT NOT NULL,
  target_kind VARCHAR(16) NOT NULL                      COMMENT 'attendance / bill / wage_sheet / payroll',
  priority SMALLINT NOT NULL DEFAULT 100,
  feature_columns JSON                                  COMMENT '特征列 AND 必含',
  feature_enabled TINYINT NOT NULL DEFAULT 0            COMMENT '0=不参与（对所有 sheet 生效）/ 1=须命中特征列',
  filter_column VARCHAR(64)                             COMMENT '过滤列',
  filter_enabled TINYINT NOT NULL DEFAULT 1             COMMENT '0=不参与 / 1=须比较 filter_column',
  mode VARCHAR(16) NOT NULL                             COMMENT 'include / exclude / gt / lt / eq / not_empty / empty',
  filter_value VARCHAR(255)                             COMMENT 'include/exclude 时关键词（逗号分隔）；gt/lt/eq 时数值',
  is_builtin TINYINT NOT NULL DEFAULT 0                 COMMENT '1=系统内置不可删，可扩展关键词',
  enabled TINYINT NOT NULL DEFAULT 1,
  match_count INT NOT NULL DEFAULT 0,
  last_matched_at DATETIME,
  note VARCHAR(255),
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_proj_kind (project_id, target_kind, enabled, priority)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='项目级行级数据有效性规则（合计行剔除/工时>0 等）';

-- ===================================================================
-- 4h. 配置：project_payroll_biz_date_rules（发薪业务日期判定，2026-05-10 新增）
-- ===================================================================
-- payroll 专有：每条发薪记录归到哪个业务周期？
--   extract: 从某列直接抽出日期（如备注'2026.4借支' → 2026-04）
--   infer:   从付款时间减偏移得业务日（如月结 N=1 月）
-- file_columns: 文件特征列（空格分隔；空=对所有 payroll 文件生效）
-- target_columns: extract 时是提取列 / infer 时是推断列（| 分隔多候选）
DROP TABLE IF EXISTS project_payroll_biz_date_rules;
CREATE TABLE project_payroll_biz_date_rules (
  id              BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  project_id      BIGINT NOT NULL,
  rule_kind       VARCHAR(16) NOT NULL                    COMMENT 'extract / infer',
  priority        SMALLINT NOT NULL DEFAULT 100,
  file_columns    VARCHAR(255)                            COMMENT '文件特征列（空格分隔；空=对所有文件生效）',
  target_columns  VARCHAR(255)                            COMMENT 'extract 提取列 / infer 推断列（| 分隔多候选）',
  offset_n        INT DEFAULT 0                           COMMENT 'infer 偏移数；extract 忽略',
  offset_unit     VARCHAR(8) DEFAULT 'day'                COMMENT 'day / month',
  enabled         TINYINT NOT NULL DEFAULT 1,
  match_count     INT NOT NULL DEFAULT 0,
  last_matched_at DATETIME,
  note            VARCHAR(255),
  created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_proj_kind_priority (project_id, rule_kind, priority, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='发薪流水业务日期判定规则（extract 优先 / infer 兜底）';

-- ===================================================================
-- 5. 配置：unit_prices（单价）
-- ===================================================================
CREATE TABLE unit_prices (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  project_id BIGINT NOT NULL,
  area VARCHAR(64) NOT NULL DEFAULT ''                 COMMENT '场地（楼层/车间），空串=通配',
  worker_type VARCHAR(64) NOT NULL DEFAULT ''          COMMENT '工种（操作员/装配工等），空串=通配',
  shift_name VARCHAR(32) NOT NULL DEFAULT ''           COMMENT '班次（白班/夜班等），空串=通配',
  price DECIMAL(8,2) NOT NULL,
  unit VARCHAR(16) NOT NULL DEFAULT '元/小时'          COMMENT '元/小时 / 元/件 / 元/单',
  effective_start DATE,
  effective_end DATE,
  note VARCHAR(255),
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_proj_eff (project_id, effective_start),
  KEY idx_match (project_id, area, worker_type, shift_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='单价表（4 维匹配：area+worker_type+shift_name+effective时间）';

-- ===================================================================
-- 6. raw 层：raw_files（所有 xlsx/pdf 文件，按内容 hash 去重单表）
-- ===================================================================
-- 设计原则（2026-05-01 拍定）：
--   - 文件级裸装，不按业务模块拆表
--   - file_hash (sha256) 是文件唯一性的唯一判断标准，文件名只是附属
--   - source_* 字段都是"上传时的归属"，仅作溯源信号，内容真实归属由 worker 解析时派生
--   - 数组字段（urls/filenames/bill_ids/project_ids）累加：同 hash 多次出现都记下来
--   - 异步 worker 程序读 parse_status='pending' 解析、识别类型、写 mart 层
CREATE TABLE raw_files (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  file_hash CHAR(64) NOT NULL                          COMMENT 'sha256，内容唯一性判断标准',
  file_size BIGINT NOT NULL,
  ai_cos_key VARCHAR(512)                              COMMENT 'ai-1306031257 桶里的 key 路径（先入 _inbox/，解析归位后改路径）',
  source_urls JSON NOT NULL                            COMMENT '同 hash 出现的所有 COS URL（数组累加）',
  source_filenames JSON NOT NULL                       COMMENT '同 hash 出现的所有 originalFileName',
  source_bill_ids JSON NOT NULL                        COMMENT '挂过的 mini_a_bill.id（不可信，仅溯源）',
  source_project_ids JSON NOT NULL                     COMMENT '上传归属项目（不可信，仅溯源）',
  first_uploaded_at DATETIME NOT NULL                  COMMENT '第一次见到该 hash 的时间（来自 url JSON.time）',
  last_seen_at DATETIME NOT NULL                       COMMENT '最近一次出现（同 hash 命中只更这个）',
  parse_status VARCHAR(16) NOT NULL DEFAULT 'pending'  COMMENT 'pending / parsed / failed / skipped',
  parse_error TEXT,
  parsed_at DATETIME,
  detected_type VARCHAR(32)                            COMMENT '考勤 / 账单 / 发薪 / 工资表 / unknown',
  ingested_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_hash (file_hash),
  KEY idx_pending (parse_status, ingested_at),
  KEY idx_type (detected_type, parse_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='raw：xlsx/pdf 文件按内容 hash 去重单表';

-- ===================================================================
-- 9. raw 层：raw_mini_user_shift_rel（fish-prod 镜像）
-- ===================================================================
CREATE TABLE raw_mini_user_shift_rel (
  etl_batch_id BIGINT NOT NULL,
  ingested_at DATETIME NOT NULL,
  source_db VARCHAR(32) NOT NULL DEFAULT 'fish-prod',
  id BIGINT UNSIGNED NOT NULL,
  user_id BIGINT,
  openid VARCHAR(150) NOT NULL DEFAULT '0',
  project_id BIGINT NOT NULL,
  shift_id BIGINT,
  shop_id INT,
  sid INT UNSIGNED NOT NULL DEFAULT 0,
  uid INT UNSIGNED,
  task_id INT UNSIGNED DEFAULT 0,
  note VARCHAR(128),
  work_amount DECIMAL(10,2),
  service_charge DECIMAL(10,2) DEFAULT 0.00,
  advance_payment_amount DECIMAL(10,2),
  tax_service_charge DECIMAL(10,2) DEFAULT 0.00,
  non_advance_payment_amount DECIMAL(10,2),
  work_pic_urls TEXT,
  work_status TINYINT UNSIGNED DEFAULT 1,
  is_source TINYINT NOT NULL DEFAULT 2,
  create_user INT UNSIGNED, create_time DATETIME,
  update_user INT UNSIGNED, update_time DATETIME,
  message VARCHAR(255),
  type TINYINT UNSIGNED NOT NULL DEFAULT 1,
  mark TINYINT UNSIGNED NOT NULL DEFAULT 1,
  sign_out_time DATETIME, pay_time DATETIME,
  alipay_reason VARCHAR(255),
  alipay_status TINYINT NOT NULL DEFAULT 0,
  out_batch_no VARCHAR(64),
  user_name VARCHAR(255),
  id_card VARCHAR(255),
  mobile VARCHAR(255),
  bank_no VARCHAR(255),
  json_ext JSON, batch_id VARCHAR(255), pdf_url VARCHAR(255),
  ent_account_id BIGINT, account_type TINYINT DEFAULT 0,
  if_sync_bill TINYINT DEFAULT 0, pay_click_time DATETIME,
  otheac VARCHAR(128),
  service_rate DECIMAL(10,2) DEFAULT 0.00,
  base_service_rate DECIMAL(10,2) DEFAULT 0.00,
  bill_status TINYINT NOT NULL DEFAULT 0,
  tax_id BIGINT, batch_pdf VARCHAR(255),
  pay_img_url VARCHAR(255), pay_img_upload_time DATETIME,
  PRIMARY KEY (id),
  KEY idx_project_paytime (project_id, pay_time),
  KEY idx_etl_batch (etl_batch_id, ingested_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='raw：fish-prod.mini_user_shift_rel 镜像（PK=id 单列，etl_batch_id 仅作"最后刷新批次"标记）';

-- ===================================================================
-- 10. raw 层：raw_mini_a_bill（fish-prod 全字段镜像）
-- ===================================================================
CREATE TABLE raw_mini_a_bill (
  etl_batch_id BIGINT NOT NULL,
  ingested_at DATETIME NOT NULL,
  source_db VARCHAR(32) NOT NULL DEFAULT 'fish-prod',
  id BIGINT NOT NULL,
  project_id BIGINT NOT NULL,
  first_name VARCHAR(255)                              COMMENT '甲方名称',
  sub_project_name VARCHAR(255)                        COMMENT '子项目名称',
  bill_month VARCHAR(255)                              COMMENT '账单月份',
  bill_interval_start DATE,
  bill_interval_end DATE,
  bill_amount DECIMAL(10,2) DEFAULT 0,
  invoice_amount DECIMAL(10,2) DEFAULT 0,
  invoicing_time DATE,
  cycle_amount DECIMAL(10,2) DEFAULT 0,
  due_refund_time DATE,
  url JSON                                             COMMENT '上传地址(JSON 数组：[{fileUrl, time, originalFileName, size}])',
  color VARCHAR(255),
  status TINYINT UNSIGNED                              COMMENT '回款状态(1已 2部分 0未)',
  advance_refund_time DATE,
  advance_refund_amount DECIMAL(10,2) DEFAULT 0,
  actual_refund_time DATE,
  actual_refund_amount DECIMAL(10,2) DEFAULT 0,
  bill_valid_amount DECIMAL(10,2) DEFAULT 0,
  verify_type TINYINT,
  note VARCHAR(255),
  bill_status TINYINT UNSIGNED                         COMMENT '账单状态(1有效 2失效)',
  create_user INT UNSIGNED,
  create_time DATETIME,
  update_user INT UNSIGNED,
  update_time DATETIME,
  mark TINYINT UNSIGNED                                COMMENT '有效标识(1正常 0删除)',
  PRIMARY KEY (id),
  KEY idx_proj_month (project_id, bill_month),
  KEY idx_etl_batch (etl_batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='raw：fish-prod.mini_a_bill 全字段纯镜像';

-- ===================================================================
-- 10b. raw 层：raw_mini_shift（fish-prod 全字段镜像）
-- ===================================================================
-- 出款计算需 JOIN raw_mini_user_shift_rel.shift_id = raw_mini_shift.id 取 title
CREATE TABLE raw_mini_shift (
  etl_batch_id BIGINT NOT NULL,
  ingested_at DATETIME NOT NULL,
  source_db VARCHAR(32) NOT NULL DEFAULT 'fish-prod',
  id BIGINT NOT NULL,
  sid INT,
  shop_id INT,
  user_id INT,
  title VARCHAR(255)                                   COMMENT '班次标题（出款计算规则1关键字段）',
  project_id BIGINT,
  is_confirm TINYINT,
  pay_status TINYINT,
  is_source TINYINT,
  area_radius INT,
  lat VARCHAR(255), lng VARCHAR(255),
  if_ele_fence TINYINT, if_face_nucleation TINYINT,
  area_name VARCHAR(255), tpl_url VARCHAR(255),
  shift_date DATE,
  shift_start DATETIME, shift_end DATETIME,
  shift_amount DECIMAL(10,2),
  shift_type TINYINT UNSIGNED                          COMMENT '1白班 2中班 3晚班 4其他',
  card_view TEXT,
  if_insure TINYINT, insure_plan BIGINT,
  address VARCHAR(255), city VARCHAR(255), district VARCHAR(255),
  province VARCHAR(255), name VARCHAR(255),
  latitude VARCHAR(255), longitude VARCHAR(255),
  create_user INT UNSIGNED, create_time DATETIME,
  update_user INT UNSIGNED, update_time DATETIME,
  version BIGINT, mark TINYINT UNSIGNED,
  task_id VARCHAR(255),
  PRIMARY KEY (id),
  KEY idx_project_date (project_id, shift_date),
  KEY idx_title (title),
  KEY idx_etl_batch (etl_batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='raw：fish-prod.mini_shift 全字段纯镜像';

-- ===================================================================
-- 12. mart 派生：attendance（考勤，按 business_month 分区）
-- ===================================================================
CREATE TABLE attendance (
  id BIGINT NOT NULL AUTO_INCREMENT,
  etl_batch_id BIGINT NOT NULL,
  ingested_at DATETIME NOT NULL,
  enterprise_id BIGINT NOT NULL,
  project_id BIGINT NOT NULL,
  worker_id BIGINT NOT NULL,
  business_month CHAR(7) NOT NULL                      COMMENT '业务月，如 2026-04',
  shift_date DATE NOT NULL,
  shift_name VARCHAR(32),
  worker_type VARCHAR(32)                              COMMENT '岗位（装卸/分拣/司机等）',
  floor_or_group VARCHAR(64)                           COMMENT '部门（A区仓/餐务部等）',
  worker_class VARCHAR(16)                             COMMENT '工人类型（临时工/长期工）',
  hours DECIMAL(6,2)                                   COMMENT '工时事实（计时项目用）；金额由程序按 unit_prices 计算，不存 DB',
  quantity DECIMAL(10,2)                               COMMENT '件数/数量（计件项目用，元/件 单价乘这个）',
  source_type VARCHAR(32) NOT NULL,
  from_bill TINYINT NOT NULL DEFAULT 0                 COMMENT '0=独立考勤文件 / 1=从账单文件解析出',
  is_valid TINYINT NOT NULL DEFAULT 1                  COMMENT '1=有效 / 0=被数据有效性规则过滤（保留供审计但 calc 不参与）',
  invalid_reason VARCHAR(255)                          COMMENT '过滤原因（哪条 validity 规则命中）',
  source_file_id BIGINT                                COMMENT '关联 raw_files.id（事件溯源）',
  source_ref VARCHAR(255)                              COMMENT '辅助溯源：sheet#row 或其他位置信息',
  name_raw VARCHAR(64),
  id_card_raw VARCHAR(64),
  extra_data JSON                                      COMMENT '未映射列归此（key=文件列名 value=单元格值）',
  PRIMARY KEY (id, business_month),
  KEY idx_proj_month (project_id, business_month, shift_date),
  KEY idx_proj_month_valid (project_id, business_month, is_valid),
  KEY idx_source_file (source_file_id),
  KEY idx_worker_month (worker_id, business_month),
  KEY idx_batch (etl_batch_id),
  KEY idx_shift_date (shift_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='mart：考勤派生表'
PARTITION BY RANGE COLUMNS(business_month) (
  PARTITION p202601 VALUES LESS THAN ('2026-02'),
  PARTITION p202602 VALUES LESS THAN ('2026-03'),
  PARTITION p202603 VALUES LESS THAN ('2026-04'),
  PARTITION p202604 VALUES LESS THAN ('2026-05'),
  PARTITION p202605 VALUES LESS THAN ('2026-06'),
  PARTITION p202606 VALUES LESS THAN ('2026-07'),
  PARTITION p202607 VALUES LESS THAN ('2026-08'),
  PARTITION p202608 VALUES LESS THAN ('2026-09'),
  PARTITION p202609 VALUES LESS THAN ('2026-10'),
  PARTITION p202610 VALUES LESS THAN ('2026-11'),
  PARTITION p202611 VALUES LESS THAN ('2026-12'),
  PARTITION p202612 VALUES LESS THAN ('2027-01'),
  PARTITION pmax VALUES LESS THAN MAXVALUE
);

-- ===================================================================
-- 12b. mart 派生：attendance_summary（月汇总考勤；甲方仅给"姓名+月总工时"无日级数据时用）
-- ===================================================================
-- 设计原则（2026-05-04 新增）：
--   - attendance 是日级事实（一行=一日），attendance_summary 是月级事实（一行=一人一月）
--   - 来源场景：甲方酒店类账单常给"月汇总考勤表"——每行一个工人，日列 '/'，末尾列是 平日工时合计/金额
--   - calc 取名单/总工时时 UNION 两表（attendance.SUM(hours) + attendance_summary.hours）
--   - 不能塞 attendance：shift_date NOT NULL 且 一行=一日 是 attendance 的不变量，月汇总硬塞会污染按 shift_date 过滤的逻辑
CREATE TABLE attendance_summary (
  id BIGINT NOT NULL AUTO_INCREMENT,
  etl_batch_id BIGINT NOT NULL,
  ingested_at DATETIME NOT NULL,
  enterprise_id BIGINT NOT NULL,
  project_id BIGINT NOT NULL,
  worker_id BIGINT NOT NULL,
  business_month CHAR(7) NOT NULL,
  hours DECIMAL(8,2)                                   COMMENT '月汇总工时（计时项目）',
  quantity DECIMAL(12,2)                               COMMENT '月汇总件数（计件项目）',
  worker_type VARCHAR(32),
  floor_or_group VARCHAR(64),
  worker_class VARCHAR(16)                             COMMENT '工人类型（临时工/长期工）',
  source_type VARCHAR(32) NOT NULL,
  from_bill TINYINT NOT NULL DEFAULT 0                 COMMENT '0=独立考勤 / 1=从账单文件解析',
  is_valid TINYINT NOT NULL DEFAULT 1                  COMMENT '1=有效 / 0=被 validity 规则过滤',
  invalid_reason VARCHAR(255),
  source_file_id BIGINT                                COMMENT '关联 raw_files.id（事件溯源）',
  source_ref VARCHAR(255)                              COMMENT '辅助溯源：sheet#row',
  name_raw VARCHAR(64),
  id_card_raw VARCHAR(64),
  extra_data JSON                                      COMMENT '未映射列归此',
  PRIMARY KEY (id, business_month),
  UNIQUE KEY uk_dedup (project_id, worker_id, business_month, source_file_id),
  KEY idx_proj_month (project_id, business_month),
  KEY idx_proj_month_valid (project_id, business_month, is_valid),
  KEY idx_worker_month (worker_id, business_month),
  KEY idx_source_file (source_file_id),
  KEY idx_batch (etl_batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='mart：考勤月汇总（月汇总专用，不污染 attendance 日级语义）'
PARTITION BY RANGE COLUMNS(business_month) (
  PARTITION p202601 VALUES LESS THAN ('2026-02'),
  PARTITION p202602 VALUES LESS THAN ('2026-03'),
  PARTITION p202603 VALUES LESS THAN ('2026-04'),
  PARTITION p202604 VALUES LESS THAN ('2026-05'),
  PARTITION p202605 VALUES LESS THAN ('2026-06'),
  PARTITION p202606 VALUES LESS THAN ('2026-07'),
  PARTITION p202607 VALUES LESS THAN ('2026-08'),
  PARTITION p202608 VALUES LESS THAN ('2026-09'),
  PARTITION p202609 VALUES LESS THAN ('2026-10'),
  PARTITION p202610 VALUES LESS THAN ('2026-11'),
  PARTITION p202611 VALUES LESS THAN ('2026-12'),
  PARTITION p202612 VALUES LESS THAN ('2027-01'),
  PARTITION pmax VALUES LESS THAN MAXVALUE
);

-- ===================================================================
-- 13a. mart 派生：bill_totals（账单总金额，每份账单一行；同月可多份）
-- ===================================================================
CREATE TABLE bill_totals (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  etl_batch_id BIGINT NOT NULL,
  ingested_at DATETIME NOT NULL,
  enterprise_id BIGINT NOT NULL,
  project_id BIGINT NOT NULL,
  business_month CHAR(7) NOT NULL,
  amount DECIMAL(14,2) NOT NULL                        COMMENT '这一份账单的金额，一行=一份；多份账单同(project_id,business_month)多行',
  source_type VARCHAR(32) NOT NULL,
  source_file_id BIGINT                                COMMENT '关联 raw_files.id',
  source_ref VARCHAR(255)                              COMMENT '辅助溯源：sheet#row 或 部门小计累加 / 综合表 等',
  KEY idx_proj_month (project_id, business_month),
  KEY idx_source_file (source_file_id),
  KEY idx_batch (etl_batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='账单总金额（每份账单一行;同月可多份）';

-- ===================================================================
-- 13b. mart 派生：bill_persons（账单人员金额；同人多份账单各算各，下游 SUM 合并）
-- ===================================================================
CREATE TABLE bill_persons (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  etl_batch_id BIGINT NOT NULL,
  ingested_at DATETIME NOT NULL,
  enterprise_id BIGINT NOT NULL,
  project_id BIGINT NOT NULL,
  worker_id BIGINT NOT NULL,
  business_month CHAR(7) NOT NULL,
  name_raw VARCHAR(64) NOT NULL,
  amount DECIMAL(12,2) NOT NULL                        COMMENT '该工人在这份账单里的金额；同人多份账单各算各',
  source_type VARCHAR(32) NOT NULL,
  source_file_id BIGINT                                COMMENT '关联 raw_files.id',
  source_ref VARCHAR(255),
  KEY idx_proj_month (project_id, business_month),
  KEY idx_proj_month_worker (project_id, business_month, worker_id),
  KEY idx_source_file (source_file_id),
  KEY idx_batch (etl_batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='账单人员金额（一行=一人在一份账单里；用于代收人封顶/无考勤项目名单替代）';

-- ===================================================================
-- 14. mart 派生：payrolls（发薪流水）
-- ===================================================================
CREATE TABLE payrolls (
  id BIGINT NOT NULL AUTO_INCREMENT,
  etl_batch_id BIGINT NOT NULL,
  ingested_at DATETIME NOT NULL,
  enterprise_id BIGINT NOT NULL,
  project_id BIGINT NOT NULL,
  worker_id BIGINT NOT NULL,
  business_month CHAR(7) NOT NULL                      COMMENT '业务月（normal 按工时日 / loan 按 pay_time 月）',
  pay_time DATETIME NOT NULL,
  parsed_shift_date DATE                               COMMENT '班次名解析的工时日（业务真实日，对借支=pay_time 当天）',
  work_amount DECIMAL(10,2) NOT NULL                   COMMENT '付款金额（出款计算取这个）',
  payroll_kind VARCHAR(32)                             COMMENT '备注：normal / loan，仅记录解析路径，不参与业务过滤',
  alipay_status TINYINT                                COMMENT '支付状态（2=已支付）',
  source_type VARCHAR(32) NOT NULL,
  source_file_id BIGINT                                COMMENT '关联 raw_files.id（事件溯源）',
  source_ref VARCHAR(255)                              COMMENT '辅助溯源：sheet#row 或其他位置信息',
  name_raw VARCHAR(64),
  id_card_raw VARCHAR(64),
  PRIMARY KEY (id, business_month),
  UNIQUE KEY uk_dedup (project_id, name_raw, pay_time, work_amount, business_month) COMMENT 'feedback_payroll_dedup 三元组去重，DB 流和 xlsx 流共写时 INSERT IGNORE 自然跳过',
  KEY idx_proj_month (project_id, business_month, pay_time),
  KEY idx_worker_month (worker_id, business_month),
  KEY idx_paytime (pay_time),
  KEY idx_batch (etl_batch_id),
  KEY idx_source_file (source_file_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='mart：发薪流水派生表'
PARTITION BY RANGE COLUMNS(business_month) (
  PARTITION p202601 VALUES LESS THAN ('2026-02'),
  PARTITION p202602 VALUES LESS THAN ('2026-03'),
  PARTITION p202603 VALUES LESS THAN ('2026-04'),
  PARTITION p202604 VALUES LESS THAN ('2026-05'),
  PARTITION p202605 VALUES LESS THAN ('2026-06'),
  PARTITION p202606 VALUES LESS THAN ('2026-07'),
  PARTITION p202607 VALUES LESS THAN ('2026-08'),
  PARTITION p202608 VALUES LESS THAN ('2026-09'),
  PARTITION p202609 VALUES LESS THAN ('2026-10'),
  PARTITION p202610 VALUES LESS THAN ('2026-11'),
  PARTITION p202611 VALUES LESS THAN ('2026-12'),
  PARTITION p202612 VALUES LESS THAN ('2027-01'),
  PARTITION pmax VALUES LESS THAN MAXVALUE
);

-- ===================================================================
-- 15. mart 派生：wage_sheets（工资表/应发清单）
-- ===================================================================
CREATE TABLE wage_sheets (
  id BIGINT NOT NULL AUTO_INCREMENT,
  etl_batch_id BIGINT NOT NULL,
  ingested_at DATETIME NOT NULL,
  enterprise_id BIGINT NOT NULL,
  project_id BIGINT NOT NULL,
  worker_id BIGINT NOT NULL,
  business_month CHAR(7) NOT NULL                      COMMENT '业务月（推断自挂载的 mini_a_bill.bill_month；工资表是劳务文员瞎编没有自证渠道）',
  payable_amount DECIMAL(12,2) NOT NULL                COMMENT '应发工资（工资表唯一可信结果数据）',
  is_substitute TINYINT NOT NULL DEFAULT 0             COMMENT '0否 1是。有的劳务会在工资表里标明代收/顶替情况',
  substitute_name VARCHAR(64)                          COMMENT '顶替对象姓名（is_substitute=1 时用）',
  source_type VARCHAR(32) NOT NULL                     COMMENT 'jiafang_wage_xlsx / huibao_zaizhi_fallback',
  source_file_id BIGINT                                COMMENT '关联 raw_files.id（事件溯源）',
  source_ref VARCHAR(255)                              COMMENT '辅助溯源：sheet#row 或其他位置信息',
  name_raw VARCHAR(64),
  PRIMARY KEY (id, business_month),
  KEY idx_proj_month (project_id, business_month),
  KEY idx_worker_month (worker_id, business_month),
  KEY idx_batch (etl_batch_id),
  KEY idx_source_file (source_file_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='mart：工资表'
PARTITION BY RANGE COLUMNS(business_month) (
  PARTITION p202601 VALUES LESS THAN ('2026-02'),
  PARTITION p202602 VALUES LESS THAN ('2026-03'),
  PARTITION p202603 VALUES LESS THAN ('2026-04'),
  PARTITION p202604 VALUES LESS THAN ('2026-05'),
  PARTITION p202605 VALUES LESS THAN ('2026-06'),
  PARTITION p202606 VALUES LESS THAN ('2026-07'),
  PARTITION p202607 VALUES LESS THAN ('2026-08'),
  PARTITION p202608 VALUES LESS THAN ('2026-09'),
  PARTITION p202609 VALUES LESS THAN ('2026-10'),
  PARTITION p202610 VALUES LESS THAN ('2026-11'),
  PARTITION p202611 VALUES LESS THAN ('2026-12'),
  PARTITION p202612 VALUES LESS THAN ('2027-01'),
  PARTITION pmax VALUES LESS THAN MAXVALUE
);

-- ===================================================================
-- 16. 字典：controllers（实控人）
-- ===================================================================
CREATE TABLE controllers (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(64) NOT NULL,
  id_card VARCHAR(18),
  mobile VARCHAR(20),
  note VARCHAR(255),
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_id_card (id_card),
  KEY idx_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='实控人字典';

-- ===================================================================
-- 17. 关系：controller_enterprise_map
-- ===================================================================
CREATE TABLE controller_enterprise_map (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  controller_id BIGINT NOT NULL,
  enterprise_id BIGINT NOT NULL,
  role VARCHAR(32) NOT NULL DEFAULT '实控人',
  note VARCHAR(255),
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_pair (controller_id, enterprise_id),
  KEY idx_ent (enterprise_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='实控人↔企业映射（多对多）';

-- ===================================================================
-- 18. 配置：credit_limits（授信）
-- ===================================================================
CREATE TABLE credit_limits (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  controller_id BIGINT NOT NULL,
  amount_yuan DECIMAL(14,2) NOT NULL                   COMMENT '授信总额（元）',
  effective_start DATE,
  effective_end DATE,
  note VARCHAR(512),
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_ctrl_eff (controller_id, effective_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='实控人授信总额';

-- ===================================================================
-- 19. 状态：temp_occupations（临时占用）
-- ===================================================================
CREATE TABLE temp_occupations (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  controller_id BIGINT NOT NULL,
  enterprise_id BIGINT,
  project_id BIGINT,
  business_month CHAR(7),
  amount_yuan DECIMAL(12,2) NOT NULL,
  applied_at DATETIME NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT '占用中'         COMMENT '占用中 / 已转正 / 已释放',
  expire_at DATETIME,
  note VARCHAR(255),
  KEY idx_ctrl_status (controller_id, status, applied_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='临时占用（24h 有效）';

-- ===================================================================
-- 20. 出款管理：loan_records（fish-prod.mini_loan_record 镜像 + 本地补全）
-- ===================================================================
CREATE TABLE loan_records (
  id BIGINT NOT NULL PRIMARY KEY                       COMMENT '直接用 fish-prod.mini_loan_record.id',
  enterprise_id BIGINT NOT NULL,
  project_id BIGINT NOT NULL,
  loan_id_str VARCHAR(64)                              COMMENT '本地出款ID（甲方-企业-项目-yyyymmdd-NNN）',
  abill_month CHAR(7)                                  COMMENT '业务周期（权威源）',
  bill_month CHAR(7),
  pay_time DATE NOT NULL,
  amount DECIMAL(12,2) NOT NULL,
  predict_time DATE, due_time DATE,
  to_be_return_amount DECIMAL(12,2) NOT NULL DEFAULT 0,
  returned_amount DECIMAL(12,2) NOT NULL DEFAULT 0,
  status TINYINT,
  mark TINYINT NOT NULL DEFAULT 1,
  source_type VARCHAR(16) NOT NULL DEFAULT 'fish-prod' COMMENT 'fish-prod 镜像 / manual 手填',
  last_synced_at DATETIME,
  note VARCHAR(255),
  KEY idx_proj_abill (project_id, abill_month, pay_time),
  KEY idx_paytime (pay_time),
  KEY idx_loan_str (loan_id_str)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='出款记录';

-- ===================================================================
-- 21. 配置：return_time_config（回款时间配置）
-- ===================================================================
CREATE TABLE return_time_config (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  project_id BIGINT NOT NULL,
  return_days INT,
  note VARCHAR(255),
  KEY idx_proj (project_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='项目回款时间配置';

-- ===================================================================
-- 22. 出款管理：return_records（回款记录）
-- ===================================================================
CREATE TABLE return_records (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  return_id_str VARCHAR(64),
  loan_id BIGINT NOT NULL,
  amount DECIMAL(12,2) NOT NULL,
  return_time DATETIME NOT NULL,
  note VARCHAR(255),
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_loan (loan_id, return_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='回款记录';

-- ===================================================================
-- 23. 代收超额：coverage_excess_detail（每笔出款的超额明细）
-- ===================================================================
CREATE TABLE coverage_excess_detail (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  loan_id BIGINT NOT NULL,
  controller_id BIGINT NOT NULL,
  enterprise_id BIGINT,
  project_id BIGINT,
  business_month CHAR(7),
  loan_amount DECIMAL(12,2) NOT NULL,
  actual_payroll DECIMAL(12,2),
  allowed_cap DECIMAL(12,2),
  excess_amount DECIMAL(12,2),
  checked_at DATETIME,
  KEY idx_ctrl_month (controller_id, business_month),
  KEY idx_loan (loan_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='代收超额明细';

-- ===================================================================
-- 24. 代收超额：coverage_offset_records（冲抵记录）
-- ===================================================================
CREATE TABLE coverage_offset_records (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  loan_id BIGINT NOT NULL,
  offset_amount DECIMAL(12,2) NOT NULL,
  offset_type VARCHAR(32) NOT NULL                     COMMENT '出款扣减 / 发薪消化 / 回款冲抵',
  offset_at DATETIME NOT NULL,
  note VARCHAR(255),
  KEY idx_loan (loan_id, offset_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='代收超额冲抵记录';

-- ===================================================================
-- 25. 代收超额：coverage_summary_by_controller（实控人维度汇总）
-- ===================================================================
CREATE TABLE coverage_summary_by_controller (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  controller_id BIGINT NOT NULL,
  total_loan DECIMAL(14,2) NOT NULL DEFAULT 0,
  total_payroll DECIMAL(14,2) NOT NULL DEFAULT 0,
  total_cap DECIMAL(14,2) NOT NULL DEFAULT 0,
  total_excess DECIMAL(14,2) NOT NULL DEFAULT 0,
  total_offset DECIMAL(14,2) NOT NULL DEFAULT 0,
  net_excess DECIMAL(14,2) NOT NULL DEFAULT 0,
  status VARCHAR(16)                                   COMMENT '正常 / 超额观察中 / 需人工介入',
  last_checked_at DATETIME,
  UNIQUE KEY uk_ctrl (controller_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='代收超额-实控人维度汇总';

-- ===================================================================
-- 初始占位行
-- ===================================================================
-- 系统级 etl_batch_id=0 占位（手工 INSERT/历史导入用）
INSERT INTO etl_batches (id, started_at, finished_at, scope_enterprise, scope_project,
                         modules, triggered_by, status, error_message)
VALUES (0, '2026-04-30 00:00:00', '2026-04-30 00:00:00', NULL, NULL,
        JSON_ARRAY(), 'system', 'ok', NULL);

SET FOREIGN_KEY_CHECKS = 1;

-- ===================================================================
-- 完成验证
-- ===================================================================
SELECT
  TABLE_SCHEMA AS db,
  COUNT(*) AS table_count
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = 'fish-test' AND TABLE_TYPE = 'BASE TABLE';
-- 期望：table_count = 25
