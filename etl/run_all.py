"""chain orchestrator：按项目跑全链 raw → mart

阶段顺序：
  1.1  load_fishprod_db.run(ent, proj)              ← raw_mini_* 镜像
  1.2  sync_cos.sync_project(proj)                  ← COS 文件镜像 + raw_files 入库
  2.A  dispatcher.main(project_id=proj)             ← 文件流：识别 + 归位 + parsers 写 mart
  2.B  standardize.payrolls.standardize(proj)       ← DB 流：raw_mini_* → mart payrolls

每步独立 etl_batch，失败立即停。calc 不在 chain 内（按需触发）。

用法：
  python etl/run_all.py --enterprise-id 588 --project-id 1879848562958331906
"""
import sys
import argparse
import time
sys.path.insert(0, 'D:/小鱼AI数据')

from etl.raw import load_fishprod_db, sync_cos
from etl import dispatcher
from etl.standardize import payrolls as std_payrolls


def run(enterprise_id, project_id, skip_db_mirror=False, skip_cos_sync=False,
        skip_dispatcher=False, skip_payrolls=False):
    """跑全链。各阶段可独立跳过用于调试。"""
    print('=' * 80)
    print(f'chain orchestrator: enterprise_id={enterprise_id} project_id={project_id}')
    print('=' * 80)

    if not skip_db_mirror:
        print('\n[1.1] DB 镜像 load_fishprod_db.run()')
        t0 = time.time()
        load_fishprod_db.run(enterprise_id=enterprise_id, project_id=project_id)
        print(f'  ✓ done in {time.time()-t0:.1f}s')

    if not skip_cos_sync:
        print('\n[1.2] COS 文件镜像 sync_cos.sync_project()')
        t0 = time.time()
        sync_cos.sync_project(project_id)
        print(f'  ✓ done in {time.time()-t0:.1f}s')

    if not skip_dispatcher:
        print('\n[2.A] 文件流 dispatcher.main()（识别+归位+parsers→mart）')
        t0 = time.time()
        dispatcher.main(project_id=project_id)
        print(f'  ✓ done in {time.time()-t0:.1f}s')

    if not skip_payrolls:
        print('\n[2.B] DB 流 standardize.payrolls.standardize()')
        t0 = time.time()
        std_payrolls.standardize(project_id)
        print(f'  ✓ done in {time.time()-t0:.1f}s')

    print('\n' + '=' * 80)
    print('chain ok ✓')
    print('=' * 80)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--enterprise-id', type=int, required=True)
    ap.add_argument('--project-id', type=int, required=True)
    ap.add_argument('--skip-db-mirror', action='store_true')
    ap.add_argument('--skip-cos-sync', action='store_true')
    ap.add_argument('--skip-dispatcher', action='store_true')
    ap.add_argument('--skip-payrolls', action='store_true')
    args = ap.parse_args()
    run(enterprise_id=args.enterprise_id, project_id=args.project_id,
        skip_db_mirror=args.skip_db_mirror, skip_cos_sync=args.skip_cos_sync,
        skip_dispatcher=args.skip_dispatcher, skip_payrolls=args.skip_payrolls)
