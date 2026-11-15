// カメラ/ファイル選択どちらの入力も、送信用の #receipt に集約してプレビュー表示する
(function () {
  const target = document.getElementById("receipt");
  const preview = document.getElementById("preview");
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
    });
  }
  take(document.getElementById("cam"));
  take(document.getElementById("pick"));
})();
