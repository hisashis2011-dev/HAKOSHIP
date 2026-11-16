// 領収書を選んだら即アップロード(1枚=1行)。複数選択・カメラ撮影どちらも同じ。
(function () {
  const form = document.getElementById("upload-form");
  if (!form) return;
  const busy = document.getElementById("busy");
  ["cam", "pick"].forEach((id) => {
    const el = document.getElementById(id);
    el.addEventListener("change", () => {
      if (!el.files.length) return;
      // もう一方の入力は空にして二重送信を防ぐ
      ["cam", "pick"].filter((x) => x !== id).forEach((x) => (document.getElementById(x).value = ""));
      busy.hidden = false;
      form.submit();
    });
  });
})();
