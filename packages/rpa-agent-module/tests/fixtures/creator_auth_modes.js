// 仅补充回环模拟页的登录与验证码场景；没有真实账号和外部请求。
if (new URL(location.href).searchParams.get("mode") === "no_login") {
  document.querySelector(".user-info").remove();
}
if (new URL(location.href).searchParams.get("mode") === "challenge") {
  const challenge = document.createElement("div");
  challenge.className = "synthetic-challenge";
  challenge.textContent = "本地合成验证码：必须人工处理";
  document.body.prepend(challenge);
}
