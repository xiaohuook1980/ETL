"""统一腾讯云 COS 客户端：从 db_config.txt 读凭据

用法：
    from scripts._cos import client, BUCKET, upload_bytes, download_bytes, exists

    upload_bytes('ocr/abc123.json', b'{"foo":"bar"}')
    data = download_bytes('ocr/abc123.json')
    if exists('ocr/abc123.json'): ...
"""
import os
import configparser
from pathlib import Path
from qcloud_cos import CosConfig, CosS3Client
from qcloud_cos.cos_exception import CosServiceError

# 默认走仓库根目录的 db_config.txt；env XIAOYU_DB_CONFIG 可覆盖
CONFIG_PATH = Path(os.environ.get(
    'XIAOYU_DB_CONFIG',
    Path(__file__).resolve().parents[1] / 'db_config.txt'
))


def _load_section(section='cos-ai'):
    cp = configparser.ConfigParser()
    cp.read(CONFIG_PATH, encoding='utf-8')
    if section not in cp:
        raise ValueError(f'db_config.txt 中没有 [{section}] section')
    return cp[section]


_s = _load_section('cos-ai')
SECRET_ID = _s['secret_id']
SECRET_KEY = _s['secret_key']
REGION = _s['region']
BUCKET = _s['bucket']

_config = CosConfig(Region=REGION, SecretId=SECRET_ID, SecretKey=SECRET_KEY)
client = CosS3Client(_config)


def upload_bytes(key: str, data: bytes, content_type=None) -> dict:
    """上传 bytes 到 COS，key 是相对桶根的路径（如 'ocr/abc.json'）"""
    kwargs = dict(Bucket=BUCKET, Key=key, Body=data)
    if content_type:
        kwargs['ContentType'] = content_type
    return client.put_object(**kwargs)


def upload_file(key: str, local_path) -> dict:
    """上传本地文件到 COS"""
    return client.upload_file(Bucket=BUCKET, Key=key, LocalFilePath=str(local_path))


def download_bytes(key: str) -> bytes:
    """下载 COS 对象为 bytes"""
    resp = client.get_object(Bucket=BUCKET, Key=key)
    return resp['Body'].get_raw_stream().read()


def download_file(key: str, local_path) -> dict:
    """下载 COS 对象到本地"""
    return client.download_file(Bucket=BUCKET, Key=key, DestFilePath=str(local_path))


def exists(key: str) -> bool:
    """检查 COS 对象是否存在"""
    try:
        client.head_object(Bucket=BUCKET, Key=key)
        return True
    except CosServiceError as e:
        if e.get_status_code() == 404:
            return False
        raise


def list_keys(prefix='', max_count=1000):
    """列出指定前缀下所有对象 key（自动翻页）"""
    keys = []
    marker = ''
    while True:
        resp = client.list_objects(Bucket=BUCKET, Prefix=prefix, Marker=marker, MaxKeys=min(1000, max_count - len(keys)))
        for it in resp.get('Contents', []):
            keys.append(it['Key'])
            if len(keys) >= max_count: return keys
        if resp.get('IsTruncated') == 'true':
            marker = resp.get('NextMarker', '')
        else:
            break
    return keys


def delete(key: str) -> dict:
    """删除单个对象"""
    return client.delete_object(Bucket=BUCKET, Key=key)


if __name__ == '__main__':
    # 自检：上传/下载/列举/删除一个测试对象
    test_key = '_self_check/probe.txt'
    payload = b'fish-test cos probe @ ' + str(__import__('datetime').datetime.now()).encode('utf-8')
    print(f'桶: {BUCKET} (region={REGION})')
    print(f'凭据: {SECRET_ID[:8]}... / {SECRET_KEY[:8]}...')
    try:
        upload_bytes(test_key, payload, content_type='text/plain')
        print(f'  ✓ 上传 {test_key} ({len(payload)}B)')
        assert exists(test_key)
        print(f'  ✓ exists() OK')
        got = download_bytes(test_key)
        assert got == payload
        print(f'  ✓ 下载内容一致 ({len(got)}B)')
        keys = list_keys(prefix='_self_check/')
        print(f'  ✓ list_keys() {keys}')
        delete(test_key)
        print(f'  ✓ 删除 {test_key}')
        assert not exists(test_key)
        print(f'  ✓ 删除后不存在')
        print('\nCOS 自检通过 ✅')
    except Exception as e:
        print(f'  ✗ {type(e).__name__}: {e}')
        raise
