"""Pull main and refresh locked dependencies; service lifecycle belongs to the operator."""
from importlib import metadata
from pathlib import Path
import subprocess
import sys


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def dependencies_match(lock):
    for line in lock.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name, expected = line.split('==', 1)
        try:
            if metadata.version(name) != expected:
                return False
        except metadata.PackageNotFoundError:
            return False
    return True


def update(root):
    root = Path(root).resolve()
    if git(root, 'status', '--porcelain'):
        raise RuntimeError('工作区有未提交改动，请先处理后重试；不会覆盖或重置文件。')
    if git(root, 'branch', '--show-current') != 'main':
        raise RuntimeError('请先切换到 main 分支；更新脚本不会自动切换分支。')
    previous = git(root, 'rev-parse', 'HEAD')
    lock = root / 'requirements.lock.txt'
    previous_lock = lock.read_bytes()
    subprocess.run(['git', '-C', str(root), 'pull', '--ff-only', 'origin', 'main'], check=True)
    current = git(root, 'rev-parse', 'HEAD')
    if previous_lock != lock.read_bytes() or not dependencies_match(lock):
        print('安装 requirements.lock.txt 中的锁定依赖…', flush=True)
        subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', str(lock)], check=True)
    else:
        print('锁定依赖未变且已安装，跳过安装。', flush=True)
    print(f'源码版本：{previous[:12]} → {current[:12]}', flush=True)
    print('更新完成。请通过现有服务管理器启动/重启；本脚本不操作服务、数据、配置或 Cloudflare Worker。', flush=True)


if __name__ == '__main__':
    try:
        update(Path(__file__).resolve().parent)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f'更新失败：{error}', file=sys.stderr)
        raise SystemExit(1)
