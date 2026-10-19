// Cấu hình chạy của web client. Backend Python phục vụ file này khi chạy local (SSE bật).
// Khi deploy qua Cloudflare Worker (deploy/cloudflare), Worker tự trả bản mode "tunnel" cho đường dẫn này.
window.OTT_CONFIG = { mode: 'local' };
