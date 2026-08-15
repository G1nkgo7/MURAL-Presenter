import brushDocument from "../public/mural-brush.html?raw";

export function MuralBrush({ language }: { language: "en" | "zh" }) {
  const localizedDocument = brushDocument
    .replaceAll("Move the brush to reveal", language === "zh" ? "移动画笔，让壁画浮现" : "Move the brush to reveal")
    .replaceAll("Reset wall", language === "zh" ? "重新粉刷" : "Reset wall");
  const documentWithLanguage = localizedDocument.replace(
    "<html lang=\"en\">",
    `<html lang="${language === "zh" ? "zh-CN" : "en"}"><base href="/mural-presenter/">`,
  );
  const autoRunDocument = documentWithLanguage.replace(
    "</body>",
    "<img src=\"data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=\" hidden onerror=\"void(0)\" onload=\"eval(document.scripts[0].textContent);this.remove()\"></body>",
  );
  const source = `data:text/html;charset=utf-8,${encodeURIComponent(autoRunDocument)}`;
  return (
    <iframe
      className="mural-brush-frame"
      src={source}
      title={language === "zh" ? "用画笔显露 MURAL 壁画" : "Reveal the MURAL wall with a paintbrush"}
    />
  );
}
