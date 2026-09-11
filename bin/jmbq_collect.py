"""
JMBQ mod 悬浮窗素材采集工具

用途：采集 mod 悬浮窗的按钮截图，生成模板供 multiplier.py 识图使用。
     模板保存到 ./assets/jmbq/，需要的四个文件：
       OVERLAY_EXPANDED.png   展开面板的固定锚点（面板标题栏/边框等稳定区域）
       OVERLAY_COLLAPSED.png  收起小球锚点
       SWITCH_ON.png          "以德服人"开关 = 开 状态按钮
       SWITCH_OFF.png         "以德服人"开关 = 关 状态按钮

用法（在 ALAS 目录下，模拟器开着 mod 版游戏）：
  toolkit\python.exe bin\jmbq_collect.py --serial 127.0.0.1:16416

流程：
  1. 自动调起悬浮窗（root adb 下 am startservice），失败则手动点开游戏菜单
  2. 依次拍摄：展开菜单(开态) -> 手动把开关切到关 -> 回车 -> 拍摄(关态) -> 手动收起 -> 回车 -> 拍摄(收起)
  3. 截图保存在 ./screenshots/jmbq/，用看图工具量出各按钮坐标后运行 --crop 生成模板：
     toolkit\python.exe bin\jmbq_collect.py --crop jmbq_expanded_on.png "x1,y1,x2,y2" OVERLAY_EXPANDED
     （OVERLAY_EXPANDED / OVERLAY_COLLAPSED / SWITCH_ON / SWITCH_OFF 依次生成）
"""
import argparse
import os
import subprocess
import sys
import time

sys.path.append('.')

from module.logger import logger  # noqa: E402

ADB = './toolkit/Lib/site-packages/adbutils/binaries/adb.exe'
SHOT_DIR = './screenshots/jmbq'
TEMPLATE_DIR = './assets/jmbq'
PACKAGE = 'com.bilibili.AzurLane'
SERVICE = 'com.android.support.Launcher'


def adb(serial, *args, timeout=15):
    cmd = [ADB]
    if serial:
        cmd += ['-s', serial]
    cmd += list(args)
    result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return result.stdout


def screencap(serial, name):
    os.makedirs(SHOT_DIR, exist_ok=True)
    path = os.path.join(SHOT_DIR, name)
    data = adb(serial, 'exec-out', 'screencap', '-p', timeout=20)
    with open(path, 'wb') as f:
        f.write(data)
    logger.info(f'saved: {path} ({len(data)} bytes)')
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--serial', default='', help='adb serial, e.g. 127.0.0.1:16416')
    parser.add_argument('--crop', nargs=3, metavar=('IMAGE', 'AREA', 'NAME'),
                        help='crop x1,y1,x2,y2 from image, save as assets/jmbq/NAME.png')
    args = parser.parse_args()

    if args.crop:
        image, area, name = args.crop
        from PIL import Image
        img = Image.open(os.path.join(SHOT_DIR, image) if not os.path.isabs(image) else image)
        x1, y1, x2, y2 = [int(v) for v in area.split(',')]
        crop = img.crop((x1, y1, x2, y2))
        os.makedirs(TEMPLATE_DIR, exist_ok=True)
        out = os.path.join(TEMPLATE_DIR, f'{name}.png')
        crop.save(out)
        logger.info(f'template saved: {out} ({crop.size[0]}x{crop.size[1]})')
        return

    serial = args.serial
    if not serial:
        devices = adb('', 'devices').decode('utf-8', errors='ignore')
        lines = [l for l in devices.splitlines() if l.endswith('device')]
        if len(lines) == 1:
            serial = lines[0].split('\t')[0]
        elif not lines:
            logger.critical('No adb device found. Start the emulator first.')
            return
        else:
            logger.critical(f'Multiple devices: {lines}, specify --serial')
            return
    logger.info(f'using device: {serial}')

    logger.info('starting overlay service (root adb) ...')
    adb(serial, 'shell', 'am', 'startservice', '-n', f'{PACKAGE}/{SERVICE}')
    time.sleep(3)
    screencap(serial, 'jmbq_overlay_appear.png')

    input('>>> 用开关把"以德服人"切到另一状态，回车后截图: ')
    screencap(serial, 'jmbq_expanded_other.png')

    input('>>> 收起悬浮窗（点收起按钮），回车后截图: ')
    screencap(serial, 'jmbq_collapsed.png')

    logger.info('done. Crop templates with --crop, see file header for usage.')
    logger.info('open status check: am startservice works = success above, else start overlay manually.')


if __name__ == '__main__':
    main()
