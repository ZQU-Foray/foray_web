/* 字体加载失败时按原分包重试一次；保留字重和字符范围，避免错用其他字形。 */
(function () {
  "use strict";
  if (!document.fonts) return;
  var attempted = new WeakSet();

  function key(family, weight, style, stretch, range) {
    return [family.replace(/["']/g, ""), weight || "normal", style || "normal",
            stretch || "normal", range || "U+0-10FFFF"]
      .join("|").replace(/\s+/g, "").toLowerCase();
  }

  function declarations() {
    var result = new Map();
    var visited = new Set();
    function collect(sheet) {
      if (!sheet || visited.has(sheet)) return;
      visited.add(sheet);
      var rules;
      try { rules = sheet.cssRules; } catch (e) { return; }
      Array.prototype.forEach.call(rules || [], function (rule) {
        if (rule instanceof CSSImportRule) {
          collect(rule.styleSheet);
        } else if (rule instanceof CSSFontFaceRule) {
          var s = rule.style;
          var family = s.getPropertyValue("font-family");
          var weight = s.getPropertyValue("font-weight") || "normal";
          var style = s.getPropertyValue("font-style") || "normal";
          var stretch = s.getPropertyValue("font-stretch") || "normal";
          var range = s.getPropertyValue("unicode-range") || "U+0-10FFFF";
          var match = /url\(["']?([^"')]+)["']?\)/.exec(s.getPropertyValue("src"));
          if (!match) return;
          result.set(key(family, weight, style, stretch, range), {
            url: new URL(match[1], sheet.href || document.baseURI).href,
            sheet: sheet, rule: rule,
            descriptors: { weight: weight, style: style, stretch: stretch, unicodeRange: range,
                           display: s.getPropertyValue("font-display") || "swap" }
          });
        }
      });
    }
    Array.prototype.forEach.call(document.styleSheets, collect);
    return result;
  }

  function repair() {
    var bad = [];
    document.fonts.forEach(function (face) {
      if (face.status === "error" && !attempted.has(face)) bad.push(face);
    });
    if (!bad.length) return;
    var decls = declarations();
    bad.forEach(function (face) {
      attempted.add(face);
      var decl = decls.get(key(face.family, face.weight, face.style, face.stretch, face.unicodeRange));
      if (!decl) return;
      try {
        var replacement = new FontFace(face.family, 'url("' + decl.url + '")', decl.descriptors);
        replacement.load().then(function (loaded) {
          document.fonts.add(loaded);
          /* 样式表绑定的字体不能直接删除；移除对应失败声明，其他分包保持不变。 */
          var index = Array.prototype.indexOf.call(decl.sheet.cssRules, decl.rule);
          if (index >= 0) decl.sheet.deleteRule(index);
          document.fonts.delete(face);
        }).catch(function () { /* 重试仍失败时保留回退字体，不循环请求。 */ });
      } catch (e) { /* 不支持字体接口的浏览器保留正常回退。 */ }
    });
  }

  document.fonts.addEventListener("loadingerror", repair);
  document.fonts.ready.then(repair);
})();
