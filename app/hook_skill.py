# -*- coding: utf-8 -*-
"""Shared prompt rules for short-form video hooks."""

TIKTOK_HOOK_3S_SKILL = """QUY TẮC HOOK TIKTOK 3 GIÂY:
- 0-1 giây: hình ảnh chặn lướt (chuyển động, kết quả, chi tiết lạ hoặc tương phản rõ).
- 1-2 giây: lời thoại/chữ tạo tò mò và khiến người xem đặt một câu hỏi cụ thể.
- 2-3 giây: mở vòng thông tin, hứa phần giải đáp có thật ở các cảnh tiếp theo.
- Tạo 5 ứng viên theo các góc khác nhau trong nhóm: hoài nghi; tại sao; so sánh/khác biệt;
  cảnh báo/phá giả định; con số cụ thể; đoán thử; đảo ngược kỳ vọng.
- Chấm từng ứng viên 0-2 điểm cho Visual, Curiosity, Gap, Clarity và Payoff; dùng ứng viên
  tổng điểm cao nhất. Nếu hòa, ưu tiên ứng viên khớp cảnh mở đầu và payoff rõ nhất.
- Hook phải trung thực với lời gốc, facts và cảnh thật; không bịa claim, không giật tít sai,
  không chào hỏi dài và không mở đầu bằng phần giới thiệu chung chung.
- Kết quả cuối phải đồng bộ: cảnh 0-1 giây, lời đọc 1-3 giây và nội dung toàn video."""

