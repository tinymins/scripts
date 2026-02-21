# Video Converter

FFmpeg + NirCmd 的 Windows 右键菜单视频转码工具。右键视频文件即可选择预设参数进行硬件加速转码（NVIDIA NVENC）。

## 安装

1. 下载 [ffmpeg](https://github.com/BtbN/FFmpeg-Builds/releases)（选 `ffmpeg-master-latest-win64`），解压到 `.vendor/ffmpeg/`。
2. 下载 [NirCmd 64-bit](https://www.nirsoft.net/utils/nircmd.html)，解压到 `.vendor/nircmd/`。
3. 以管理员权限运行 `install.bat` 注册右键菜单。

## 转码预设

| 脚本 | 容器 | 编码 | 码率 | 说明 |
|------|------|------|------|------|
| `converter_mkv_h264_vbr_18000_25000_seq.bat` | MKV | H.264 | 18000k/25000k VBR | 2-pass NVENC |
| `converter_mkv_h265_vbr_10000_15000_seq.bat` | MKV | H.265 | 10000k/15000k VBR | 2-pass NVENC |
| `converter_mkv_h265_vbr_18000_25000_seq.bat` | MKV | H.265 | 18000k/25000k VBR | 2-pass NVENC |
| `converter_mp4_h264_vbr_10000_15000_seq.bat` | MP4 | H.264 | 18000k/25000k VBR | 2-pass NVENC, 60fps |
| `converter_mp4_h264_vbr_18000_25000_seq.bat` | MP4 | H.264 | 18000k/25000k VBR | 2-pass NVENC, 60fps |
| `converter_crop_seq.bat` | MP4 | — | — | 裁剪 1280x590 (offset 0:130) |

转码完成后自动通过 NirCmd 克隆源文件时间戳。
