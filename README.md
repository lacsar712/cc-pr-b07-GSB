# 印刷套准复核台

接口只把印张偏差放进待处理队列。另一个进程用行锁领走一条，算出套准或套不准后再写回。页面每隔一秒看一次，直到结论出现。

## 端口

| 服务 | 地址 |
|------|------|
| 页面 | http://localhost:3194 |
| 接口 | http://localhost:8194 |
| PostgreSQL | localhost:54394 |

## 账号

| 用户 | 密码 | 权限 |
|------|------|------|
| printer | print123456 | 可送复核 |
| checker | check123456 | 只看 |

## 启动

```bash
cd projects/15-print-register-review
docker compose up --build
```

## 验收

1. printer 登录后稍等，封面-01 变成套准，内页-09 变成套不准。
2. 再送一条青偏差 0.5 的印张，状态先是待处理，随后变成套不准。
3. checker 没有送复核按钮。
