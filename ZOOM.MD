# Trình tạo video Zoom và Pan

Dự án này cho phép bạn tạo video với hiệu ứng zoom và pan dựa trên tệp cấu hình đã cho.

## Tính năng

- Tải các điểm từ tệp cấu hình JSON.
- Tạo video với hiệu ứng zoom và pan dựa trên các điểm đã tải.

## Cách sử dụng

1. Tạo môi trường ảo:
   ##### Trên Windows
   ```bash
   python -m venv zoomenv
   .\zoomenv\Scripts\activate
   ```

   ##### Trên Linux/MacOS
   ```bash
   python3 -m venv zoomenv
   source zoomenv/bin/activate
   ```

2. Cài đặt các thư viện cần thiết:
   ```bash
   pip install -r zoom_requirements.txt
   ```

3. Chạy chương trình:
   ```bash
   python .\src\zoom.py --input_image <đường_dẫn_đến_ảnh_đầu_vào> --config <đường_dẫn_đến_tệp_cấu_hình> --output <đường_dẫn_đến_video_đầu_ra> --fps <tốc_độ_khung_hình> --device <thiết_bị_mã_hóa>
   ```

   Ví dụ:
   ```bash
   python .\src\zoom.py --input_image demos\input.jpg --config demos\config.json --output demos\output.mp4 --fps 30 --device gpu
   ```

## Lưu ý: 
### Kiểm tra FFMPEG có hỗ trợ NVENC hay không
##### Trên Windows
```bash
ffmpeg -codecs | findstr nvenc
```

##### Trên Linux/MacOS
```bash
ffmpeg -codecs | grep nvenc
```
### Kiểm tra GPU có hoạt động hay không
```bash
nvidia-smi
```
