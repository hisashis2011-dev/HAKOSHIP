// カメラ/ファイル選択どちらの入力も、送信用の #receipt に集約してプレビュー表示する
(function () {
  const target = document.getElementById("receipt");
  const preview = document.getElementById("preview");
  const form = document.getElementById("expense-form");
  const status = document.getElementById("ocr-status");
  // AIによる自動入力。失敗・無効でも手入力でそのまま申請できる。入力済みの欄は上書きしない。
  async function autofill(file) {
    if (form.dataset.ocr !== "1") return;
    status.textContent = "⏳ 領収書を読み取り中…(読み取り中も入力できます)";
    try {
      const fd = new FormData();
      fd.append("receipt", file);
      fd.append("csrf_token", form.querySelector("[name=csrf_token]").value);
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), 30000);
      const res = await fetch("/expenses/ocr", { method: "POST", body: fd, signal: ctl.signal });
      clearTimeout(timer);
      const j = await res.json();
      if (!j.ok) throw new Error("no result");
      let n = 0;
      for (const [k, v] of Object.entries(j.fields)) {
        const el = form.querySelector(`[name=${k}]`);
        if (el && !el.value.trim()) { el.value = v; n++; }
      }
      status.textContent = n ? "✅ " + n + "項目を自動入力しました。必ず内容を確認してください。"
                             : "入力済みの項目があるため自動入力はしませんでした。";
    } catch (e) {
      status.textContent = "自動読み取りができませんでした。お手数ですが手入力してください。";
    }
  }
  function take(input) {
    input.addEventListener("change", () => {
      const f = input.files[0];
      if (!f) return;
      const dt = new DataTransfer();
      dt.items.add(f);
      target.files = dt.files;
      preview.innerHTML = "";
      if (f.type.startsWith("image/")) {
        const img = document.createElement("img");
        img.src = URL.createObjectURL(f);
        preview.appendChild(img);
      } else {
        preview.textContent = "選択中: " + f.name;
      }
      autofill(f);
    });
  }
  take(document.getElementById("cam"));
  take(document.getElementById("pick"));
})();
