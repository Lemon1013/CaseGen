import os
import base64

ASSETS_DIRS = [
    r"C:\Users\Administrator\.gemini\antigravity\brain\9b1e5c97-01ac-4f31-8609-fd8b320060f2\ppt_assets",
    r"c:\Users\Administrator\ZCodeProject\CaseGen\ppt_assets"
]

OUT_PATHS = [
    r"c:\Users\Administrator\ZCodeProject\CaseGen\casegen_presentation.html",
    r"C:\Users\Administrator\.gemini\antigravity\brain\9b1e5c97-01ac-4f31-8609-fd8b320060f2\casegen_presentation.html"
]

def get_base64_image(filename):
    for d in ASSETS_DIRS:
        p = os.path.join(d, filename)
        if os.path.exists(p):
            with open(p, "rb") as f:
                data = f.read()
            b64 = base64.b64encode(data).decode('utf-8')
            return f"data:image/png;base64,{b64}"
    raise FileNotFoundError(f"Asset not found: {filename} in {ASSETS_DIRS}")

def main():
    print("Loading screenshots into Base64...")
    img_workbench = get_base64_image("ppt_workbench.png")
    img_task_flow = get_base64_image("ppt_task_flow.png")
    img_prompts = get_base64_image("ppt_prompts.png")
    img_wiki = get_base64_image("ppt_wiki.png")
    img_documents = get_base64_image("ppt_documents.png")
    img_external_modal = get_base64_image("ppt_external_wiki_modal.png")
    img_external = get_base64_image("ppt_external_wiki.png")
    img_cases = get_base64_image("ppt_cases.png")
    print("All 8 images loaded successfully.")

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>CaseGen 测试用例生成平台 · 产品演示</title>
  <style>
    :root {{
      --bg-base: #06080d;
      --bg-surface: #0e121a;
      --bg-card: rgba(14, 18, 27, 0.88);
      --bg-card-hover: rgba(22, 28, 42, 0.96);
      --border-base: rgba(255, 255, 255, 0.08);
      --border-strong: rgba(255, 255, 255, 0.15);
      --border-emerald: rgba(16, 185, 129, 0.35);
      --emerald-500: #10b981;
      --emerald-400: #34d399;
      --emerald-300: #6ee7b7;
      --emerald-glow: rgba(16, 185, 129, 0.2);
      --emerald-subtle: rgba(16, 185, 129, 0.1);
      --cyan-400: #22d3ee;
      --text-primary: #f8fafc;
      --text-secondary: #94a3b8;
      --text-muted: #64748b;
      --font-sans: -apple-system, BlinkMacSystemFont, "SF Pro Display", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Segoe UI", sans-serif;
      --font-mono: "JetBrains Mono", "SFMono-Regular", Consolas, Menlo, monospace;
    }}

    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }}

    body {{
      background-color: var(--bg-base);
      color: var(--text-primary);
      font-family: var(--font-sans);
      overflow: hidden;
      width: 100vw;
      height: 100vh;
      user-select: none;
      -webkit-font-smoothing: antialiased;
    }}

    /* 现代星轨与矩阵背景光晕 */
    .cinema-bg {{
      position: fixed;
      inset: 0;
      background-image: 
        radial-gradient(rgba(16, 185, 129, 0.06) 1px, transparent 1px),
        radial-gradient(circle at 50% -10%, rgba(16, 185, 129, 0.12), transparent 55%),
        radial-gradient(circle at 90% 100%, rgba(34, 211, 238, 0.05), transparent 45%);
      background-size: 32px 32px, 100% 100%, 100% 100%;
      pointer-events: none;
      z-index: 0;
    }}

    /* 顶部发光进度指示条 */
    .top-progress-bar {{
      position: fixed;
      top: 0;
      left: 0;
      height: 3px;
      background: linear-gradient(90deg, #059669, #10b981, #34d399, #22d3ee);
      box-shadow: 0 0 14px rgba(16, 185, 129, 0.7);
      width: 12.5%;
      transition: width 0.3s cubic-bezier(0.16, 1, 0.3, 1);
      z-index: 100;
    }}

    /* 幻灯片容器 */
    .slide-deck {{
      position: relative;
      width: 100vw;
      height: 100vh;
      z-index: 1;
    }}

    .slide {{
      display: none;
      position: absolute;
      inset: 0;
      width: 100vw;
      height: 100vh;
      padding: 14px 28px 56px 28px;
      align-items: center;
      justify-content: center;
      opacity: 0;
      transition: opacity 0.25s cubic-bezier(0.16, 1, 0.3, 1), transform 0.25s cubic-bezier(0.16, 1, 0.3, 1);
      transform: scale(0.996) translateY(3px);
    }}

    .slide.active {{
      display: flex;
      opacity: 1;
      transform: scale(1) translateY(0);
    }}

    /* ========================================================
       标准双列黄金网格容器 (.canvas-container)
       width: 95vw; max-width: 1840px; height: calc(100vh - 64px); max-height: 1020px;
       ======================================================== */
    .canvas-container {{
      width: 95vw;
      max-width: 1840px;
      height: calc(100vh - 64px);
      max-height: 1020px;
      margin: 0 auto;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 16px;
    }}

    /* 顶部标题区 (36px 大标题、18px 说明、右侧页码徽标) */
    .slide-header {{
      flex-shrink: 0;
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      padding: 0 4px;
    }}

    .slide-header-left {{
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}

    .slide-tag {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      font-size: 13px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--emerald-400);
      background: rgba(16, 185, 129, 0.12);
      border: 1px solid rgba(16, 185, 129, 0.3);
      padding: 3px 12px;
      border-radius: 9999px;
      width: fit-content;
    }}

    .pulse-dot {{
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--emerald-400);
      box-shadow: 0 0 8px var(--emerald-400);
      animation: pulse 2s infinite;
    }}

    @keyframes pulse {{
      0%, 100% {{ opacity: 1; transform: scale(1); }}
      50% {{ opacity: 0.4; transform: scale(0.85); }}
    }}

    .slide-title-group {{
      display: flex;
      align-items: baseline;
      gap: 16px;
      flex-wrap: wrap;
    }}

    .slide-title {{
      font-size: 36px;
      font-weight: 800;
      letter-spacing: -0.015em;
      color: #ffffff;
      line-height: 1.2;
      text-shadow: 0 2px 10px rgba(0, 0, 0, 0.5);
    }}

    .slide-subtitle {{
      font-size: 18px;
      color: var(--text-secondary);
      font-weight: 400;
      line-height: 1.4;
    }}

    .slide-header-right {{
      display: flex;
      align-items: center;
      gap: 12px;
      flex-shrink: 0;
      padding-top: 4px;
    }}

    .slide-badge-num {{
      font-family: var(--font-mono);
      font-size: 14px;
      font-weight: 700;
      color: var(--emerald-300);
      background: rgba(16, 185, 129, 0.12);
      border: 1px solid rgba(16, 185, 129, 0.35);
      padding: 4px 14px;
      border-radius: 9999px;
    }}

    /* ========================================================
       主体栅格 (.slide-grid)：左侧文字卡片 + 右侧 4K 截图窗口
       display: grid; grid-template-columns: minmax(480px, 1.15fr) minmax(780px, 1.85fr); gap: 36px;
       ======================================================== */
    .slide-grid {{
      display: grid;
      grid-template-columns: minmax(480px, 1.15fr) minmax(780px, 1.85fr);
      gap: 36px;
      flex: 1;
      min-height: 0;
      align-items: stretch;
    }}

    /* 左侧列：文字卡片全部垂直排列，flex: 1 均衡铺满 */
    .col-left {{
      display: flex;
      flex-direction: column;
      gap: 14px;
      height: 100%;
      min-height: 0;
    }}

    .feature-card {{
      background: var(--bg-card);
      border: 1px solid var(--border-base);
      border-radius: 12px;
      padding: 16px 22px;
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 8px;
      flex: 1;
      backdrop-filter: blur(12px);
      transition: all 0.22s cubic-bezier(0.16, 1, 0.3, 1);
      position: relative;
      overflow: hidden;
    }}

    .feature-card::before {{
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      width: 3px;
      height: 100%;
      background: transparent;
      transition: background 0.2s ease;
    }}

    .feature-card:hover {{
      background: var(--bg-card-hover);
      border-color: var(--border-emerald);
      transform: translateX(4px);
      box-shadow: 0 10px 24px rgba(0, 0, 0, 0.4), 0 0 16px var(--emerald-glow);
    }}

    .feature-card:hover::before {{
      background: var(--emerald-400);
    }}

    .feature-card.highlight {{
      background: rgba(16, 185, 129, 0.09);
      border-color: rgba(16, 185, 129, 0.4);
      box-shadow: 0 0 20px rgba(16, 185, 129, 0.12);
    }}

    .feature-card.highlight::before {{
      background: var(--emerald-400);
    }}

    .card-top {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}

    .feature-index {{
      font-family: var(--font-mono);
      font-size: 18px;
      font-weight: 800;
      color: var(--emerald-400);
      background: rgba(16, 185, 129, 0.14);
      border: 1px solid rgba(16, 185, 129, 0.35);
      padding: 2px 10px;
      border-radius: 6px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      line-height: 1.2;
    }}

    .feature-card.highlight .feature-index {{
      background: var(--emerald-500);
      color: #06080d;
      font-weight: 900;
      box-shadow: 0 0 10px rgba(16, 185, 129, 0.5);
    }}

    .feature-title {{
      font-size: 19px;
      font-weight: 700;
      color: #ffffff;
      letter-spacing: -0.01em;
      display: flex;
      align-items: center;
      gap: 8px;
    }}

    .feature-tag-badge {{
      font-size: 11px;
      font-weight: 600;
      color: var(--emerald-300);
      background: rgba(16, 185, 129, 0.18);
      border: 1px solid rgba(16, 185, 129, 0.3);
      padding: 1px 7px;
      border-radius: 4px;
    }}

    .feature-desc {{
      font-size: 15px;
      color: #e2e8f0;
      line-height: 1.65;
      font-weight: 400;
    }}

    /* 右侧列：4K 高清截图展示窗口 */
    .col-right {{
      height: 100%;
      min-height: 0;
      display: flex;
      flex-direction: column;
    }}

    .showcase-window {{
      width: 100%;
      height: 100%;
      min-height: 0;
      display: flex;
      flex-direction: column;
      background: #080a10;
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 12px;
      box-shadow: 0 20px 50px rgba(0, 0, 0, 0.75), 0 0 1px rgba(255, 255, 255, 0.2);
      overflow: hidden;
      position: relative;
    }}

    .window-topbar {{
      height: 42px;
      background: rgba(14, 18, 27, 0.96);
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 16px;
      flex-shrink: 0;
      z-index: 5;
    }}

    .traffic-lights {{
      display: flex;
      align-items: center;
      gap: 7px;
    }}

    .traffic-dot {{
      width: 11px;
      height: 11px;
      border-radius: 50%;
    }}

    .dot-red {{ background: #ef4444; }}
    .dot-yellow {{ background: #f59e0b; }}
    .dot-green {{ background: #10b981; }}

    .window-title {{
      font-size: 13px;
      font-weight: 600;
      color: #94a3b8;
      font-family: var(--font-mono);
      margin-left: 12px;
    }}

    .window-topbar-right {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}

    .window-tabs {{
      display: flex;
      gap: 4px;
      background: rgba(0, 0, 0, 0.4);
      padding: 2px;
      border-radius: 6px;
      border: 1px solid rgba(255, 255, 255, 0.08);
    }}

    .window-tab {{
      background: transparent;
      border: none;
      color: #94a3b8;
      font-size: 12px;
      font-weight: 500;
      padding: 3px 12px;
      border-radius: 5px;
      cursor: pointer;
      transition: all 0.2s ease;
      font-family: var(--font-sans);
      display: flex;
      align-items: center;
      gap: 6px;
    }}

    .window-tab:hover {{
      color: #f1f5f9;
      background: rgba(255, 255, 255, 0.06);
    }}

    .window-tab.active {{
      background: rgba(16, 185, 129, 0.22);
      color: var(--emerald-300);
      font-weight: 600;
      border: 1px solid rgba(16, 185, 129, 0.45);
      box-shadow: 0 0 10px rgba(16, 185, 129, 0.25);
    }}

    .zoom-btn {{
      background: rgba(255, 255, 255, 0.06);
      border: 1px solid rgba(255, 255, 255, 0.14);
      color: #cbd5e1;
      font-size: 12px;
      padding: 4px 12px;
      border-radius: 6px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 5px;
      transition: all 0.2s ease;
    }}

    .zoom-btn:hover {{
      background: rgba(16, 185, 129, 0.2);
      border-color: rgba(16, 185, 129, 0.45);
      color: #ffffff;
    }}

    .window-body {{
      flex: 1;
      min-height: 0;
      width: 100%;
      height: 100%;
      position: relative;
      background: #ffffff;
      display: flex;
      align-items: flex-start;
      justify-content: center;
      overflow-y: auto;
      overflow-x: hidden;
      cursor: zoom-in;
      scrollbar-width: thin;
      scrollbar-color: rgba(16, 185, 129, 0.35) transparent;
    }}

    .window-body::-webkit-scrollbar {{
      width: 6px;
    }}
    .window-body::-webkit-scrollbar-thumb {{
      background: rgba(16, 185, 129, 0.35);
      border-radius: 3px;
    }}

    .window-pane {{
      display: none;
      width: 100%;
      min-height: 100%;
      background: #ffffff;
      position: relative;
    }}

    .window-pane.active {{
      display: block;
      animation: paneFade 0.2s ease-out;
    }}

    @keyframes paneFade {{
      from {{ opacity: 0; }}
      to {{ opacity: 1; }}
    }}

    .screenshot-img {{
      width: 100%;
      height: auto;
      display: block;
      object-fit: contain;
      object-position: top center;
      image-rendering: -webkit-optimize-contrast;
      image-rendering: crisp-edges;
    }}

    .zoom-hover-badge {{
      position: absolute;
      bottom: 12px;
      right: 14px;
      background: rgba(10, 14, 23, 0.88);
      border: 1px solid rgba(255, 255, 255, 0.16);
      color: #cbd5e1;
      font-size: 11px;
      padding: 4px 10px;
      border-radius: 6px;
      backdrop-filter: blur(10px);
      box-shadow: 0 4px 14px rgba(0, 0, 0, 0.6);
      display: flex;
      align-items: center;
      gap: 5px;
      pointer-events: none;
      opacity: 0.8;
      transition: opacity 0.2s ease, transform 0.2s ease;
      z-index: 10;
    }}

    .window-body:hover .zoom-hover-badge {{
      opacity: 1;
      transform: translateY(-2px);
      border-color: rgba(16, 185, 129, 0.4);
      color: var(--emerald-300);
    }}

    .window-footer {{
      height: 34px;
      background: rgba(11, 14, 22, 0.95);
      border-top: 1px solid rgba(255, 255, 255, 0.07);
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 16px;
      flex-shrink: 0;
      font-size: 12px;
      color: var(--text-secondary);
    }}

    .window-caption {{
      font-size: 12px;
      color: #cbd5e1;
      display: flex;
      align-items: center;
      gap: 6px;
    }}

    .window-tag {{
      font-family: var(--font-mono);
      font-size: 11px;
      color: var(--emerald-400);
      background: rgba(16, 185, 129, 0.1);
      padding: 1px 7px;
      border-radius: 4px;
    }}

    /* ========================================================
       Slide 01: 首页 / 封面版式
       ======================================================== */
    .cover-container {{
      width: 95vw;
      max-width: 1700px;
      height: calc(100vh - 66px);
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      padding: 8px 0;
    }}

    .cover-hero {{
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
      gap: 12px;
      margin-top: 8px;
    }}

    .cover-badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      font-weight: 700;
      color: var(--emerald-300);
      background: rgba(16, 185, 129, 0.12);
      border: 1px solid rgba(16, 185, 129, 0.35);
      padding: 5px 16px;
      border-radius: 9999px;
      letter-spacing: 0.05em;
    }}

    .cover-title {{
      font-size: 46px;
      font-weight: 900;
      letter-spacing: -0.025em;
      line-height: 1.15;
      background: linear-gradient(135deg, #ffffff 40%, #94a3b8 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      max-width: 1100px;
    }}

    .cover-title .brand-highlight {{
      background: linear-gradient(135deg, #34d399 20%, #22d3ee 80%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }}

    .cover-tagline {{
      font-size: 17px;
      color: #cbd5e1;
      max-width: 900px;
      line-height: 1.5;
    }}

    .cover-matrix-grid {{
      display: grid;
      grid-template-columns: repeat(6, 1fr);
      gap: 12px;
      margin: 10px 0;
    }}

    .matrix-card {{
      background: var(--bg-card);
      border: 1px solid var(--border-base);
      border-radius: 10px;
      padding: 16px 14px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      transition: all 0.22s cubic-bezier(0.16, 1, 0.3, 1);
      cursor: pointer;
      position: relative;
      overflow: hidden;
    }}

    .matrix-card::before {{
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 2px;
      background: linear-gradient(90deg, transparent, var(--emerald-500), transparent);
      opacity: 0;
      transition: opacity 0.22s ease;
    }}

    .matrix-card:hover {{
      background: var(--bg-card-hover);
      border-color: var(--border-emerald);
      transform: translateY(-3px);
      box-shadow: 0 10px 24px rgba(0, 0, 0, 0.6), 0 0 16px var(--emerald-glow);
    }}

    .matrix-card:hover::before {{
      opacity: 1;
    }}

    .card-step-num {{
      font-family: var(--font-mono);
      font-size: 11px;
      font-weight: 700;
      color: var(--emerald-400);
      background: rgba(16, 185, 129, 0.1);
      display: inline-block;
      padding: 2px 7px;
      border-radius: 4px;
      width: fit-content;
    }}

    .card-icon {{
      font-size: 24px;
      margin: 2px 0;
    }}

    .card-title {{
      font-size: 14px;
      font-weight: 700;
      color: #ffffff;
      line-height: 1.3;
    }}

    .card-desc {{
      font-size: 12px;
      color: var(--text-secondary);
      line-height: 1.45;
    }}

    .cover-footer {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 12px 20px;
      background: rgba(14, 17, 24, 0.7);
      border: 1px solid var(--border-base);
      border-radius: 10px;
    }}

    .cover-action-btn {{
      background: linear-gradient(135deg, #059669, #10b981);
      border: 1px solid rgba(255, 255, 255, 0.2);
      color: #ffffff;
      font-size: 14px;
      font-weight: 600;
      padding: 9px 24px;
      border-radius: 8px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      box-shadow: 0 4px 18px rgba(16, 185, 129, 0.35);
      transition: all 0.2s ease;
    }}

    .cover-action-btn:hover {{
      background: linear-gradient(135deg, #10b981, #34d399);
      box-shadow: 0 6px 24px rgba(16, 185, 129, 0.5);
      transform: translateY(-1px);
    }}

    /* ========================================================
       Slide 08: 总结与核心价值（零虚构数据，对比矩阵与能力支柱）
       ======================================================== */
    .summary-grid {{
      display: grid;
      grid-template-columns: minmax(500px, 1.15fr) minmax(500px, 1fr);
      gap: 32px;
      flex: 1;
      min-height: 0;
      align-items: stretch;
    }}

    .compare-panel, .pillars-panel {{
      background: var(--bg-card);
      border: 1px solid var(--border-base);
      border-radius: 12px;
      padding: 18px 22px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 12px;
      backdrop-filter: blur(12px);
      box-shadow: 0 16px 40px rgba(0, 0, 0, 0.5);
      overflow-y: auto;
      scrollbar-width: thin;
      scrollbar-color: rgba(16, 185, 129, 0.3) transparent;
    }}

    .compare-panel::-webkit-scrollbar, .pillars-panel::-webkit-scrollbar {{
      width: 5px;
    }}
    .compare-panel::-webkit-scrollbar-thumb, .pillars-panel::-webkit-scrollbar-thumb {{
      background: rgba(16, 185, 129, 0.3);
      border-radius: 3px;
    }}

    .panel-header-title {{
      font-size: 17px;
      font-weight: 700;
      color: #ffffff;
      display: flex;
      align-items: center;
      gap: 8px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--border-base);
      flex-shrink: 0;
    }}

    .compare-list, .pillars-list {{
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 10px;
      flex: 1;
    }}

    .compare-row {{
      flex: 1;
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 6px;
      background: rgba(0, 0, 0, 0.35);
      padding: 10px 12px;
      border-radius: 8px;
      border: 1px solid rgba(255, 255, 255, 0.06);
      transition: all 0.2s ease;
    }}

    .compare-row:hover {{
      border-color: rgba(16, 185, 129, 0.3);
      background: rgba(16, 185, 129, 0.03);
    }}

    .compare-aspect {{
      display: flex;
      align-items: center;
      gap: 8px;
      padding-bottom: 5px;
      border-bottom: 1px dashed rgba(255, 255, 255, 0.07);
    }}

    .aspect-badge {{
      font-family: var(--font-mono);
      font-size: 11px;
      font-weight: 700;
      color: var(--emerald-400);
      background: rgba(16, 185, 129, 0.12);
      border: 1px solid rgba(16, 185, 129, 0.25);
      padding: 1px 7px;
      border-radius: 4px;
    }}

    .aspect-name {{
      font-size: 13px;
      font-weight: 700;
      color: #f8fafc;
    }}

    .compare-cols {{
      display: grid;
      grid-template-columns: 1fr 1.15fr;
      gap: 10px;
    }}

    .compare-old {{
      color: #94a3b8;
      font-size: 12px;
      line-height: 1.45;
      background: rgba(239, 68, 68, 0.04);
      padding: 7px 10px;
      border-radius: 6px;
      border-left: 2px solid rgba(239, 68, 68, 0.4);
    }}

    .compare-old-title {{
      color: #f87171;
      font-weight: 600;
      font-size: 12px;
      margin-bottom: 3px;
    }}

    .compare-new {{
      color: #f1f5f9;
      font-size: 12px;
      line-height: 1.45;
      background: rgba(16, 185, 129, 0.05);
      padding: 7px 10px;
      border-radius: 6px;
      border-left: 2px solid var(--emerald-500);
    }}

    .compare-new-title {{
      color: var(--emerald-300);
      font-weight: 600;
      font-size: 12px;
      margin-bottom: 3px;
    }}

    /* 右侧 4 大能力支柱（纯功能总结，无虚构数据） */
    .pillar-card {{
      flex: 1;
      background: rgba(255, 255, 255, 0.025);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 8px;
      padding: 10px 14px;
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 5px;
      transition: all 0.2s ease;
    }}

    .pillar-card:hover {{
      background: rgba(16, 185, 129, 0.06);
      border-color: rgba(16, 185, 129, 0.35);
      transform: translateY(-1px);
    }}

    .pillar-top {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}

    .pillar-badge {{
      font-family: var(--font-mono);
      font-size: 11px;
      font-weight: 700;
      color: var(--emerald-300);
      background: rgba(16, 185, 129, 0.14);
      border: 1px solid rgba(16, 185, 129, 0.3);
      padding: 2px 7px;
      border-radius: 4px;
    }}

    .pillar-name {{
      font-size: 14px;
      font-weight: 700;
      color: #ffffff;
    }}

    .pillar-desc {{
      font-size: 12px;
      color: #cbd5e1;
      line-height: 1.45;
    }}

    .pillar-tags {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-top: 1px;
    }}

    .pillar-tag {{
      font-size: 11px;
      color: #94a3b8;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 255, 255, 0.08);
      padding: 1px 7px;
      border-radius: 9999px;
    }}

    .summary-bottom-actions {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-top: 6px;
      flex-shrink: 0;
    }}

    /* ========================================================
       底部浮动控制器与翻页栏
       ======================================================== */
    .bottom-bar {{
      position: fixed;
      bottom: 12px;
      left: 50%;
      transform: translateX(-50%);
      height: 42px;
      background: rgba(15, 18, 26, 0.9);
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 22px;
      display: flex;
      align-items: center;
      padding: 0 16px;
      gap: 14px;
      backdrop-filter: blur(16px);
      box-shadow: 0 10px 28px rgba(0, 0, 0, 0.65);
      z-index: 100;
    }}

    .bottom-btn {{
      background: transparent;
      border: none;
      color: #cbd5e1;
      font-size: 12px;
      font-weight: 500;
      padding: 5px 12px;
      border-radius: 6px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      transition: all 0.18s ease;
      font-family: var(--font-sans);
    }}

    .bottom-btn:hover {{
      background: rgba(255, 255, 255, 0.1);
      color: #ffffff;
    }}

    .bottom-btn.primary {{
      background: var(--emerald-500);
      color: #090a0f;
      font-weight: 600;
    }}

    .bottom-btn.primary:hover {{
      background: var(--emerald-400);
    }}

    .slide-counter {{
      font-family: var(--font-mono);
      font-size: 13px;
      font-weight: 700;
      color: var(--emerald-300);
      background: rgba(16, 185, 129, 0.14);
      border: 1px solid rgba(16, 185, 129, 0.35);
      padding: 3px 12px;
      border-radius: 10px;
      min-width: 72px;
      text-align: center;
    }}

    .bar-divider {{
      width: 1px;
      height: 16px;
      background: rgba(255, 255, 255, 0.14);
    }}

    /* ========================================================
       大图 Lightbox 弹出放大模态窗
       ======================================================== */
    .lightbox-modal {{
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(4, 6, 10, 0.95);
      backdrop-filter: blur(20px);
      z-index: 999;
      flex-direction: column;
      opacity: 0;
      transition: opacity 0.25s ease;
    }}

    .lightbox-modal.active {{
      display: flex;
      opacity: 1;
    }}

    .lightbox-topbar {{
      height: 50px;
      background: rgba(18, 22, 32, 0.9);
      border-bottom: 1px solid rgba(255, 255, 255, 0.1);
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 20px;
      flex-shrink: 0;
    }}

    .lightbox-title {{
      font-size: 15px;
      font-weight: 600;
      color: #f1f5f9;
      display: flex;
      align-items: center;
      gap: 10px;
    }}

    .lightbox-hint {{
      font-size: 11px;
      color: var(--text-muted);
      font-family: var(--font-mono);
    }}

    .lightbox-controls {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}

    .lightbox-btn {{
      background: rgba(255, 255, 255, 0.08);
      border: 1px solid rgba(255, 255, 255, 0.15);
      color: #ffffff;
      padding: 5px 12px;
      border-radius: 5px;
      font-size: 12px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 5px;
      transition: all 0.2s ease;
    }}

    .lightbox-btn:hover {{
      background: rgba(255, 255, 255, 0.18);
    }}

    .lightbox-btn.close {{
      background: rgba(239, 68, 68, 0.2);
      border-color: rgba(239, 68, 68, 0.4);
      color: #fca5a5;
    }}

    .lightbox-btn.close:hover {{
      background: rgba(239, 68, 68, 0.35);
      color: #ffffff;
    }}

    .lightbox-body {{
      flex: 1;
      overflow: auto;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 16px;
    }}

    .lightbox-body img {{
      max-width: 98vw;
      max-height: 90vh;
      object-fit: contain;
      border-radius: 6px;
      box-shadow: 0 16px 50px rgba(0, 0, 0, 0.85), 0 0 1px rgba(255, 255, 255, 0.3);
      cursor: grab;
      transition: transform 0.2s ease;
    }}

    /* ========================================================
       快速导航抽屉面板（Overview Drawer）
       ======================================================== */
    .drawer-overlay {{
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.75);
      backdrop-filter: blur(8px);
      z-index: 500;
      opacity: 0;
      transition: opacity 0.25s ease;
    }}

    .drawer-overlay.active {{
      display: block;
      opacity: 1;
    }}

    .nav-drawer {{
      position: fixed;
      bottom: 0;
      left: 0;
      right: 0;
      background: rgba(14, 18, 26, 0.96);
      border-top: 1px solid var(--border-emerald);
      padding: 20px 32px 28px 32px;
      z-index: 501;
      transform: translateY(100%);
      transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
      box-shadow: 0 -12px 40px rgba(0, 0, 0, 0.8);
    }}

    .nav-drawer.active {{
      transform: translateY(0);
    }}

    .drawer-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
    }}

    .drawer-title {{
      font-size: 16px;
      font-weight: 700;
      color: #ffffff;
      display: flex;
      align-items: center;
      gap: 8px;
    }}

    .drawer-grid {{
      display: grid;
      grid-template-columns: repeat(8, 1fr);
      gap: 12px;
    }}

    .drawer-card {{
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 8px;
      padding: 10px 8px;
      cursor: pointer;
      display: flex;
      flex-direction: column;
      gap: 5px;
      transition: all 0.2s ease;
      text-align: left;
    }}

    .drawer-card:hover {{
      background: rgba(16, 185, 129, 0.12);
      border-color: var(--border-emerald);
      transform: translateY(-2px);
    }}

    .drawer-card.current {{
      background: rgba(16, 185, 129, 0.2);
      border-color: var(--emerald-400);
      box-shadow: 0 0 12px rgba(16, 185, 129, 0.25);
    }}

    .drawer-card-num {{
      font-family: var(--font-mono);
      font-size: 11px;
      font-weight: 700;
      color: var(--emerald-400);
    }}

    .drawer-card-title {{
      font-size: 12px;
      font-weight: 600;
      color: #e2e8f0;
      line-height: 1.35;
    }}
  </style>
</head>
<body>
  <!-- 背景光晕与星轨 -->
  <div class="cinema-bg"></div>

  <!-- 顶部细微进度条 -->
  <div class="top-progress-bar" id="progressBar"></div>

  <!-- 幻灯片主体视窗 -->
  <main class="slide-deck">

    <!-- ========================================================
         SLIDE 01: 首页封面 · CaseGen 产品全景
         ======================================================== -->
    <section class="slide active" id="slide-0" data-index="0">
      <div class="cover-container">
        <div class="cover-hero">
          <div class="cover-badge">
            <span class="pulse-dot"></span>
            CASEGEN · SPEC ENGINE 规范工程平台
          </div>
          <h1 class="cover-title">
            基于大模型与规范工程的<br/>
            <span class="brand-highlight">企业级测试用例生成及全生命周期治理平台</span>
          </h1>
          <p class="cover-tagline">
            打通「需求智能分析 ➔ 业务规章切片 ➔ 知识库构建 ➔ Checkpoint 人工把关 ➔ 矩阵生成 ➔ 平台直连交付」全链路，让 AI 成为严密可靠的测试生产力
          </p>
        </div>

        <div class="cover-matrix-grid">
          <div class="matrix-card" onclick="goToSlide(1)">
            <span class="card-step-num">FEATURE 01</span>
            <div class="card-icon">📚</div>
            <div class="card-title">Wiki 知识库构建</div>
            <div class="card-desc">PDF/Word 规章切片入库，AI 自动提炼结构化业务规则，构建坚实合规证据底座</div>
          </div>

          <div class="matrix-card" onclick="goToSlide(2)">
            <span class="card-step-num">FEATURE 02</span>
            <div class="card-icon">🔌</div>
            <div class="card-title">外部知识库接入</div>
            <div class="card-desc">标准 REST 协议即插即用，零侵入连通企业 Confluence、既有知识库与模型网关</div>
          </div>

          <div class="matrix-card" onclick="goToSlide(3)">
            <span class="card-step-num">FEATURE 03</span>
            <div class="card-icon">🧠</div>
            <div class="card-title">智能需求分析</div>
            <div class="card-desc">支持长篇 PRD 一键解析提炼核心目标与测试边界，提供精简/标准/全面三档粒度策略</div>
          </div>

          <div class="matrix-card" onclick="goToSlide(4)">
            <span class="card-step-num">FEATURE 04</span>
            <div class="card-icon">⚡</div>
            <div class="card-title">用例生成全流程</div>
            <div class="card-desc">5 步受控流水线，测试点 Checkpoint 人工确认把关，杜绝大模型盲目幻觉扩散</div>
          </div>

          <div class="matrix-card" onclick="goToSlide(5)">
            <span class="card-step-num">FEATURE 05</span>
            <div class="card-icon">💎</div>
            <div class="card-title">用例管理与交付</div>
            <div class="card-desc">树状卡片资产化沉淀，支持证据双向穿透联动、一键复制流转及测试平台直接推送</div>
          </div>

          <div class="matrix-card" onclick="goToSlide(6)">
            <span class="card-step-num">FEATURE 06</span>
            <div class="card-icon">📝</div>
            <div class="card-title">提示词资产管理</div>
            <div class="card-desc">提示词多版本演进管理，任务引用自动锁定防误删，保障用例生成历史可重现</div>
          </div>
        </div>

        <div class="cover-footer">
          <div style="font-size: 13px; color: #94a3b8; display: flex; align-items: center; gap: 14px;">
            <span>💡 快捷提示：使用键盘 <span style="font-family: var(--font-mono); background: rgba(255,255,255,0.06); padding: 2px 7px; border-radius: 4px; border: 1px solid rgba(255,255,255,0.1);">→</span> / <span style="font-family: var(--font-mono); background: rgba(255,255,255,0.06); padding: 2px 7px; border-radius: 4px; border: 1px solid rgba(255,255,255,0.1);">Space</span> 翻页，按 <span style="font-family: var(--font-mono); background: rgba(255,255,255,0.06); padding: 2px 7px; border-radius: 4px; border: 1px solid rgba(255,255,255,0.1);">F</span> 全屏展示，按 <span style="font-family: var(--font-mono); background: rgba(255,255,255,0.06); padding: 2px 7px; border-radius: 4px; border: 1px solid rgba(255,255,255,0.1);">M</span> 打开导航目录</span>
          </div>
          <button class="cover-action-btn" onclick="nextSlide()">
            <span>开启沉浸式演示之旅</span>
            <span>➔</span>
          </button>
        </div>
      </div>
    </section>


    <!-- ========================================================
         SLIDE 02: 知识库构建 · 规章切片与结构化 Wiki 空间 (支持 Tab 切换)
         ======================================================== -->
    <section class="slide" id="slide-1" data-index="1">
      <div class="canvas-container">
        <header class="slide-header">
          <div class="slide-header-left">
            <div class="slide-tag">
              <span class="pulse-dot"></span>
              FEATURE 01 · 知识库构建
            </div>
            <div class="slide-title-group">
              <h2 class="slide-title">知识库的构建 · 规章切片与结构化 Wiki 空间</h2>
              <p class="slide-subtitle">支持 PDF/Word 规章制度一键切片，AI 自动提炼结构化业务规则页面，形成坚实测试依据底座</p>
            </div>
          </div>
          <div class="slide-header-right">
            <span class="slide-badge-num">02 / 08</span>
          </div>
        </header>

        <div class="slide-grid">
          <!-- 左侧：功能点卡片垂直排列 -->
          <div class="col-left">
            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">01</span>
                <span class="feature-title">文档解析切片</span>
              </div>
              <p class="feature-desc">支持业务规章文档（PDF/Word/TXT）批量上传，智能分块切片并自动提取段落语义摘要。</p>
            </div>

            <div class="feature-card highlight">
              <div class="card-top">
                <span class="feature-index">★ 02</span>
                <span class="feature-title">
                  沉淀业务规则 Wiki
                  <span class="feature-tag-badge">结构化</span>
                </span>
              </div>
              <p class="feature-desc">基于大模型智能提炼规章制度核心条款，沉淀为结构化、可复用、条理清晰的业务规则 Wiki 页面。</p>
            </div>

            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">03</span>
                <span class="feature-title">全链路引用追溯</span>
              </div>
              <p class="feature-desc">测试用例步骤与断言精准反向链接至 Wiki 原文段落与原始文档切片，点击即达，满足合规审计。</p>
            </div>

            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">04</span>
                <span class="feature-title">自动关联匹配</span>
              </div>
              <p class="feature-desc">用例生成任务根据需求标题与标签自动进行语义与关键词混合检索，高召回精准命中相关领域规章。</p>
            </div>
          </div>

          <!-- 右侧：4K 高清截图展示窗口 (双 Tab 切换) -->
          <div class="col-right">
            <div class="showcase-window">
              <div class="window-topbar">
                <div class="traffic-lights">
                  <span class="traffic-dot dot-red"></span>
                  <span class="traffic-dot dot-yellow"></span>
                  <span class="traffic-dot dot-green"></span>
                  <span class="window-title">Knowledge · 规章知识库与文档切片</span>
                </div>
                <div class="window-topbar-right">
                  <div class="window-tabs">
                    <button class="window-tab active" onclick="switchWindowTab(this, 's5-pane-1')">
                      <span>📚 结构化 Wiki 空间</span>
                    </button>
                    <button class="window-tab" onclick="switchWindowTab(this, 's5-pane-2')">
                      <span>📄 原始文档解析切片</span>
                    </button>
                  </div>
                  <button class="zoom-btn" onclick="openCurrentWindowTabLightbox('s5-window-body')">
                    <span>🔍 点击放大</span>
                  </button>
                </div>
              </div>

              <div class="window-body" id="s5-window-body" onclick="openCurrentWindowTabLightbox('s5-window-body')">
                <div class="window-pane active" id="s5-pane-1" data-title="Wiki 知识空间浏览 · 结构化业务规则展示">
                  <img src="{img_wiki}" alt="Wiki 知识空间浏览" class="screenshot-img" />
                </div>
                <div class="window-pane" id="s5-pane-2" data-title="业务规章文档切片与解析 · 原文切片与语义段落">
                  <img src="{img_documents}" alt="业务规章文档切片与解析" class="screenshot-img" />
                </div>
                <div class="zoom-hover-badge">
                  <span>🔍 点击查看 100% 原始 4K 高清大图（支持顶栏 Tab 切换视图）</span>
                </div>
              </div>

              <div class="window-footer">
                <div class="window-caption">
                  <span>✦ 规章制度分块切片与结构化业务 Wiki 知识沉淀</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>


    <!-- ========================================================
         SLIDE 03: 外部知识库接入 · 零侵入连通企业已有知识库 (支持 Tab 切换)
         ======================================================== -->
    <section class="slide" id="slide-2" data-index="2">
      <div class="canvas-container">
        <header class="slide-header">
          <div class="slide-header-left">
            <div class="slide-tag">
              <span class="pulse-dot"></span>
              FEATURE 02 · 外部接口接入
            </div>
            <div class="slide-title-group">
              <h2 class="slide-title">外部接口接入 · 零侵入连通企业已有知识库</h2>
              <p class="slide-subtitle">无需数据搬迁，通过标准 REST 协议即插即用接入企业 Confluence、既有知识库与模型网关</p>
            </div>
          </div>
          <div class="slide-header-right">
            <span class="slide-badge-num">03 / 08</span>
          </div>
        </header>

        <div class="slide-grid">
          <!-- 左侧：功能点卡片垂直排列 -->
          <div class="col-left">
            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">01</span>
                <span class="feature-title">一键配置连接</span>
              </div>
              <p class="feature-desc">配置服务端点 URL 与鉴权 Token，零代码侵入快速连通企业既有 Confluence 或自研知识库系统。</p>
            </div>

            <div class="feature-card highlight">
              <div class="card-top">
                <span class="feature-index">★ 02</span>
                <span class="feature-title">
                  连通性测试与选项目
                  <span class="feature-tag-badge">一键 Ping</span>
                </span>
              </div>
              <p class="feature-desc">提供一键连通性 Ping 检测，实时拉取并绑定远程可用项目空间，支持项目级知识安全隔离。</p>
            </div>

            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">03</span>
                <span class="feature-title">知识协同检索 [E#]</span>
              </div>
              <p class="feature-desc">生成流水线检索知识时并发调用外部接口，返回结果打标 [E#] 与本地 Wiki 知识库无缝融合召回。</p>
            </div>

            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">04</span>
                <span class="feature-title">异常安全降级</span>
              </div>
              <p class="feature-desc">外部接口网络超时或服务不可用时，系统自动触发熔断降级，依赖本地知识库继续生成，保障链路稳定。</p>
            </div>
          </div>

          <!-- 右侧：4K 高清截图展示窗口 (双 Tab 切换) -->
          <div class="col-right">
            <div class="showcase-window">
              <div class="window-topbar">
                <div class="traffic-lights">
                  <span class="traffic-dot dot-red"></span>
                  <span class="traffic-dot dot-yellow"></span>
                  <span class="traffic-dot dot-green"></span>
                  <span class="window-title">Integration · 外部知识库端点配置</span>
                </div>
                <div class="window-topbar-right">
                  <div class="window-tabs">
                    <button class="window-tab active" onclick="switchWindowTab(this, 's6-pane-1')">
                      <span>🔌 外部源配置弹窗</span>
                    </button>
                    <button class="window-tab" onclick="switchWindowTab(this, 's6-pane-2')">
                      <span>📋 知识空间管理列表</span>
                    </button>
                  </div>
                  <button class="zoom-btn" onclick="openCurrentWindowTabLightbox('s6-window-body')">
                    <span>🔍 点击放大</span>
                  </button>
                </div>
              </div>

              <div class="window-body" id="s6-window-body" onclick="openCurrentWindowTabLightbox('s6-window-body')">
                <div class="window-pane active" id="s6-pane-1" data-title="外部知识库配置弹窗 · 端点与鉴权连通性检测">
                  <img src="{img_external_modal}" alt="外部知识库配置弹窗" class="screenshot-img" />
                </div>
                <div class="window-pane" id="s6-pane-2" data-title="空间管理列表 · 外部知识库绑定">
                  <img src="{img_external}" alt="空间管理列表" class="screenshot-img" />
                </div>
                <div class="zoom-hover-badge">
                  <span>🔍 点击查看 100% 原始 4K 高清大图（支持顶栏 Tab 切换视图）</span>
                </div>
              </div>

              <div class="window-footer">
                <div class="window-caption">
                  <span>✦ 标准 REST 接口配置与项目空间连通性检测</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>


    <!-- ========================================================
         SLIDE 04: 智能需求分析 · 自动梳理业务目标与测试边界
         ======================================================== -->
    <section class="slide" id="slide-3" data-index="3">
      <div class="canvas-container">
        <header class="slide-header">
          <div class="slide-header-left">
            <div class="slide-tag">
              <span class="pulse-dot"></span>
              FEATURE 03 · 智能需求分析
            </div>
            <div class="slide-title-group">
              <h2 class="slide-title">智能需求分析 · 自动梳理业务目标与测试边界</h2>
              <p class="slide-subtitle">支持直接粘贴长篇 PRD，AI 自动提炼核心目标与测试边界，分析结果可直接修改微调</p>
            </div>
          </div>
          <div class="slide-header-right">
            <span class="slide-badge-num">04 / 08</span>
          </div>
        </header>

        <div class="slide-grid">
          <!-- 左侧：功能点卡片垂直排列 -->
          <div class="col-left">
            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">01</span>
                <span class="feature-title">需求粘贴输入</span>
              </div>
              <p class="feature-desc">支持长篇 PRD、用户故事或业务描述一键粘贴，无需繁琐格式预处理即可快速建单。</p>
            </div>

            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">02</span>
                <span class="feature-title">一键智能分析</span>
              </div>
              <p class="feature-desc">点击「✦ 智能需求分析」，AI 自动化进行深层语义解析，提炼出清晰的业务实体与操作脉络。</p>
            </div>

            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">03</span>
                <span class="feature-title">业务目标与边界提取</span>
              </div>
              <p class="feature-desc">自动提取核心业务目标、关键流转路径与测试边界条件，并将结构化结果实时回填至输入框。</p>
            </div>

            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">04</span>
                <span class="feature-title">人工自由微调</span>
              </div>
              <p class="feature-desc">分析结果完全透明可见，测试专家可直接在输入框编辑微调，确认无误后再提交，受控高效。</p>
            </div>
          </div>

          <!-- 右侧：4K 高清截图展示窗口 -->
          <div class="col-right">
            <div class="showcase-window">
              <div class="window-topbar">
                <div class="traffic-lights">
                  <span class="traffic-dot dot-red"></span>
                  <span class="traffic-dot dot-yellow"></span>
                  <span class="traffic-dot dot-green"></span>
                  <span class="window-title">Workbench · 需求输入与优化工作台</span>
                </div>
                <div class="window-topbar-right">
                  <button class="zoom-btn" onclick="openLightbox('img-workbench', '测试设计工作台 · 智能需求分析与生成策略配置')">
                    <span>🔍 点击放大</span>
                  </button>
                </div>
              </div>

              <div class="window-body" onclick="openLightbox('img-workbench', '测试设计工作台 · 智能需求分析与生成策略配置')">
                <div class="window-pane active">
                  <img id="img-workbench" src="{img_workbench}" alt="测试设计工作台 · 需求输入与优化" class="screenshot-img" />
                </div>
                <div class="zoom-hover-badge">
                  <span>🔍 点击查看 100% 原始 4K 高清大图</span>
                </div>
              </div>

              <div class="window-footer">
                <div class="window-caption">
                  <span>✦ 智能需求分析与三档生成粒度配置（精简 / 标准 / 全面）</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>


    <!-- ========================================================
         SLIDE 05: 用例生成全流程 · 5 步递进、人机测试点把关
         ======================================================== -->
    <section class="slide" id="slide-4" data-index="4">
      <div class="canvas-container">
        <header class="slide-header">
          <div class="slide-header-left">
            <div class="slide-tag">
              <span class="pulse-dot"></span>
              FEATURE 04 · 用例生成全流程
            </div>
            <div class="slide-title-group">
              <h2 class="slide-title">用例生成全流程 · 5 步递进、人机测试点把关</h2>
              <p class="slide-subtitle">从需求分析到生成用例，每一步清晰可见、人工可把关，彻底杜绝大模型幻觉扩散</p>
            </div>
          </div>
          <div class="slide-header-right">
            <span class="slide-badge-num">05 / 08</span>
          </div>
        </header>

        <div class="slide-grid">
          <!-- 左侧：功能点卡片垂直排列 -->
          <div class="col-left">
            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">01</span>
                <span class="feature-title">智能需求分析</span>
              </div>
              <p class="feature-desc">深度语义解析输入需求，自动理清测试目标与边界约束，夯实用例生成基石。</p>
            </div>

            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">02</span>
                <span class="feature-title">三档用例粒度</span>
              </div>
              <p class="feature-desc">精简（3~5点）、标准（5~8点）、全面（8~12+点）深度策略随心可选，兼顾敏捷与严谨。</p>
            </div>

            <div class="feature-card highlight">
              <div class="card-top">
                <span class="feature-index">★ 03</span>
                <span class="feature-title">
                  人工确认测试点
                  <span class="feature-tag-badge">前置把关</span>
                </span>
              </div>
              <p class="feature-desc">生成结构化测试点清单供人工勾选增删（Checkpoint 把关），在输出用例前杜绝盲目幻觉扩散。</p>
            </div>

            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">04</span>
                <span class="feature-title">逐点生成用例</span>
              </div>
              <p class="feature-desc">针对每个已确认的测试点独立流式生成，逐条产出完备前置条件、详细操作步骤与明确预期断言。</p>
            </div>

            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">05</span>
                <span class="feature-title">自动质量评审</span>
              </div>
              <p class="feature-desc">全流程评审分析器自动对完整性与可执行性打分，输出诊断结论与条款优化建议，形成闭环。</p>
            </div>
          </div>

          <!-- 右侧：4K 高清截图展示窗口 -->
          <div class="col-right">
            <div class="showcase-window">
              <div class="window-topbar">
                <div class="traffic-lights">
                  <span class="traffic-dot dot-red"></span>
                  <span class="traffic-dot dot-yellow"></span>
                  <span class="traffic-dot dot-green"></span>
                  <span class="window-title">Task Detail · 用例流水线全流程任务</span>
                </div>
                <div class="window-topbar-right">
                  <button class="zoom-btn" onclick="openLightbox('img-task-flow', '任务详情 · 证券交易异常处置用例流水线与测试点 Checkpoint')">
                    <span>🔍 点击放大</span>
                  </button>
                </div>
              </div>

              <div class="window-body" onclick="openLightbox('img-task-flow', '任务详情 · 证券交易异常处置用例流水线与测试点 Checkpoint')">
                <div class="window-pane active">
                  <img id="img-task-flow" src="{img_task_flow}" alt="用例生成全流程任务详情" class="screenshot-img" />
                </div>
                <div class="zoom-hover-badge">
                  <span>🔍 点击查看 100% 原始 4K 高清大图</span>
                </div>
              </div>

              <div class="window-footer">
                <div class="window-caption">
                  <span>✦ 人工勾选确认测试点清单，有效杜绝幻觉扩散</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>


    <!-- ========================================================
         SLIDE 06: 用例管理与交付 · 树状卡片与一键复制流转
         ======================================================== -->
    <section class="slide" id="slide-5" data-index="5">
      <div class="canvas-container">
        <header class="slide-header">
          <div class="slide-header-left">
            <div class="slide-tag">
              <span class="pulse-dot"></span>
              FEATURE 05 · 用例管理与交付
            </div>
            <div class="slide-title-group">
              <h2 class="slide-title">用例管理与展示 · 树状卡片与一键复制流转</h2>
              <p class="slide-subtitle">按需求归档版本化用例资产，结构化展示测试目标、步骤与断言，支持关联证据穿透查看</p>
            </div>
          </div>
          <div class="slide-header-right">
            <span class="slide-badge-num">06 / 08</span>
          </div>
        </header>

        <div class="slide-grid">
          <!-- 左侧：功能点卡片垂直排列 -->
          <div class="col-left">
            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">01</span>
                <span class="feature-title">双模式灵活查看</span>
              </div>
              <p class="feature-desc">支持树状分组卡片与 Markdown 原文双模式切换，兼顾日常测试执行与跨平台文档导出。</p>
            </div>

            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">02</span>
                <span class="feature-title">测试点与用例层级对应</span>
              </div>
              <p class="feature-desc">清晰呈现测试点（TP）与衍生测试用例（TC）的继承关系，层次分明，测试覆盖一目了然。</p>
            </div>

            <div class="feature-card highlight">
              <div class="card-top">
                <span class="feature-index">★ 03</span>
                <span class="feature-title">
                  步骤与断言卡片
                  <span class="feature-tag-badge">结构化</span>
                </span>
              </div>
              <p class="feature-desc">结构化呈现前置条件、操作步骤与预期断言，清晰标注操作要点与规章依据，执行无歧义。</p>
            </div>

            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">04</span>
                <span class="feature-title">单条一键复制为 Markdown</span>
              </div>
              <p class="feature-desc">支持单条用例或全量用例一键复制为标准 Markdown 格式，可无缝粘贴至 Jira、TAPD 等敏捷工具。</p>
            </div>

            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">05</span>
                <span class="feature-title">项目用例资产库</span>
              </div>
              <p class="feature-desc">用例按需求与版本持久化归档沉淀，支持全局标签过滤、关键词搜索与全生命周期版本溯源。</p>
            </div>
          </div>

          <!-- 右侧：4K 高清截图展示窗口 -->
          <div class="col-right">
            <div class="showcase-window">
              <div class="window-topbar">
                <div class="traffic-lights">
                  <span class="traffic-dot dot-red"></span>
                  <span class="traffic-dot dot-yellow"></span>
                  <span class="traffic-dot dot-green"></span>
                  <span class="window-title">Cases · 树状用例资产管理与交付</span>
                </div>
                <div class="window-topbar-right">
                  <button class="zoom-btn" onclick="openLightbox('img-cases', '用例管理中心 · 树状卡片资产与交付')">
                    <span>🔍 点击放大</span>
                  </button>
                </div>
              </div>

              <div class="window-body" onclick="openLightbox('img-cases', '用例管理中心 · 树状卡片资产与交付')">
                <div class="window-pane active">
                  <img id="img-cases" src="{img_cases}" alt="用例管理中心界面" class="screenshot-img" />
                </div>
                <div class="zoom-hover-badge">
                  <span>🔍 点击查看 100% 原始 4K 高清大图</span>
                </div>
              </div>

              <div class="window-footer">
                <div class="window-caption">
                  <span>✦ 结构化测试用例卡片与单条一键复制交付</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>


    <!-- ========================================================
         SLIDE 07: 提示词管理 · 规范化 Prompt 资产沉淀与选用
         ======================================================== -->
    <section class="slide" id="slide-6" data-index="6">
      <div class="canvas-container">
        <header class="slide-header">
          <div class="slide-header-left">
            <div class="slide-tag">
              <span class="pulse-dot"></span>
              FEATURE 06 · 提示词管理
            </div>
            <div class="slide-title-group">
              <h2 class="slide-title">提示词管理 · 规范化 Prompt 资产沉淀与选用</h2>
              <p class="slide-subtitle">核心生成/评审提示词模板集中版本化治理，任务引用自动锁定防误删，保障生成可重现</p>
            </div>
          </div>
          <div class="slide-header-right">
            <span class="slide-badge-num">07 / 08</span>
          </div>
        </header>

        <div class="slide-grid">
          <!-- 左侧：功能点卡片垂直排列 -->
          <div class="col-left">
            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">01</span>
                <span class="feature-title">全流程模板维护</span>
              </div>
              <p class="feature-desc">用例生成、质量评审、测试点提取、需求分析等各环节 Prompt 集中分类维护与统一管理。</p>
            </div>

            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">02</span>
                <span class="feature-title">版本化迭代演进</span>
              </div>
              <p class="feature-desc">支持多版本演进（v1, v2...），Markdown 格式自由编辑，语法变量高亮呈现，新旧版本平滑过渡。</p>
            </div>

            <div class="feature-card highlight">
              <div class="card-top">
                <span class="feature-index">★ 03</span>
                <span class="feature-title">
                  历史任务引用防误删锁定
                  <span class="feature-tag-badge">安全锁</span>
                </span>
              </div>
              <p class="feature-desc">已被任务或评审引用的 Prompt 自动置为安全保护状态，强约束防止误删，确保历史用例 100% 可重现。</p>
            </div>

            <div class="feature-card">
              <div class="card-top">
                <span class="feature-index">04</span>
                <span class="feature-title">工作台按需选用</span>
              </div>
              <p class="feature-desc">测试设计工作台在创建生成任务时，可按需灵活选用指定版本的提示词，便于开展 AB 测试与调优。</p>
            </div>
          </div>

          <!-- 右侧：4K 高清截图展示窗口 -->
          <div class="col-right">
            <div class="showcase-window">
              <div class="window-topbar">
                <div class="traffic-lights">
                  <span class="traffic-dot dot-red"></span>
                  <span class="traffic-dot dot-yellow"></span>
                  <span class="traffic-dot dot-green"></span>
                  <span class="window-title">Prompts · 规范化提示词资产治理</span>
                </div>
                <div class="window-topbar-right">
                  <button class="zoom-btn" onclick="openLightbox('img-prompts', '提示词管理 · 全流程 Prompt 模板集中治理与防误删保护')">
                    <span>🔍 点击放大</span>
                  </button>
                </div>
              </div>

              <div class="window-body" onclick="openLightbox('img-prompts', '提示词管理 · 全流程 Prompt 模板集中治理与防误删保护')">
                <div class="window-pane active">
                  <img id="img-prompts" src="{img_prompts}" alt="提示词管理界面" class="screenshot-img" />
                </div>
                <div class="zoom-hover-badge">
                  <span>🔍 点击查看 100% 原始 4K 高清大图</span>
                </div>
              </div>

              <div class="window-footer">
                <div class="window-caption">
                  <span>✦ 提示词版本演进管理与历史任务引用防误删保护机制</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>


    <!-- ========================================================
         SLIDE 08: 总结与核心价值 · 让 AI 成为可靠的测试生产力
         ======================================================== -->
    <section class="slide" id="slide-7" data-index="7">
      <div class="canvas-container">
        <header class="slide-header">
          <div class="slide-header-left">
            <div class="slide-tag">
              <span class="pulse-dot"></span>
              SUMMARY · 核心价值
            </div>
            <div class="slide-title-group">
              <h2 class="slide-title">让 AI 成为可靠的测试生产力</h2>
              <p class="slide-subtitle">超越传统黑盒 Prompt 对话：建立规范化、可追溯、可审计的企业级智能测试新范式</p>
            </div>
          </div>
          <div class="slide-header-right">
            <span class="slide-badge-num">08 / 08</span>
          </div>
        </header>

        <div class="summary-grid">
          <!-- 左侧：4 项核心差异直观对比 -->
          <div class="compare-panel">
            <div class="panel-header-title">
              <span>⚖️ 传统黑盒对话 vs CaseGen 规范工程平台</span>
            </div>

            <div class="compare-list">
              <!-- 对比 1: 输入方式 -->
              <div class="compare-row">
                <div class="compare-aspect">
                  <span class="aspect-badge">维度 01</span>
                  <span class="aspect-name">输入方式与依据</span>
                </div>
                <div class="compare-cols">
                  <div class="compare-old">
                    <div class="compare-old-title">✕ 传统对话式 AI (黑盒生成)</div>
                    <div>基于单次孤立对话输入，缺乏项目上下文与规章支撑，极易产生严重业务幻觉与事实偏差。</div>
                  </div>
                  <div class="compare-new">
                    <div class="compare-new-title">✓ CaseGen 规范工程平台</div>
                    <div>深度结合领域规章切片与 Wiki 原文精准索引，每条用例生成皆有法可依、依据确凿。</div>
                  </div>
                </div>
              </div>

              <!-- 对比 2: 测试点把关 -->
              <div class="compare-row">
                <div class="compare-aspect">
                  <span class="aspect-badge">维度 02</span>
                  <span class="aspect-name">测试点质量把关</span>
                </div>
                <div class="compare-cols">
                  <div class="compare-old">
                    <div class="compare-old-title">✕ 一步到位黑盒输出 (不可控)</div>
                    <div>直接生成大篇幅用例，测试点不可见，人工 Review 负担沉重且难以准确把控覆盖质量。</div>
                  </div>
                  <div class="compare-new">
                    <div class="compare-new-title">✓ 测试点 Checkpoint 前置把关</div>
                    <div>生成前输出结构化测试点清单，测试工程师把关确认后再触发矩阵生成，实现严密人机协同。</div>
                  </div>
                </div>
              </div>

              <!-- 对比 3: 提示词治理 -->
              <div class="compare-row">
                <div class="compare-aspect">
                  <span class="aspect-badge">维度 03</span>
                  <span class="aspect-name">提示词资产治理</span>
                </div>
                <div class="compare-cols">
                  <div class="compare-old">
                    <div class="compare-old-title">✕ 零散临时 Prompt (无版本治理)</div>
                    <div>个人随手在对话框粘贴 Prompt，缺乏集中版本管控，不可追溯且无法复现历史用例。</div>
                  </div>
                  <div class="compare-new">
                    <div class="compare-new-title">✓ 项目级 Prompt 资产治理与防误删</div>
                    <div>全流程提示词集中多版本管理，任务引用后自动锁定保护，防止误删，保障 100% 可重现。</div>
                  </div>
                </div>
              </div>

              <!-- 对比 4: 规则依据 -->
              <div class="compare-row">
                <div class="compare-aspect">
                  <span class="aspect-badge">维度 04</span>
                  <span class="aspect-name">规则可溯与审计</span>
                </div>
                <div class="compare-cols">
                  <div class="compare-old">
                    <div class="compare-old-title">✕ 缺乏规则溯源 (不可审计)</div>
                    <div>用例断言缺乏与原始规章条文的直接映射，合规审查困难，规章变更时无法定向维护。</div>
                  </div>
                  <div class="compare-new">
                    <div class="compare-new-title">✓ 知识全链路穿透与审计留痕</div>
                    <div>每条用例断言均与 Wiki 规章段落双向穿透联动，生成依据透明可信，随时可追溯审计。</div>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- 右侧：平台 4 大能力支柱总结（纯功能，不带任何虚构数据） -->
          <div class="pillars-panel">
            <div class="panel-header-title">
              <span>🏛️ 平台 4 大核心能力支柱</span>
            </div>

            <div class="pillars-list">
              <!-- 支柱 1 -->
              <div class="pillar-card">
                <div class="pillar-top">
                  <span class="pillar-badge">支柱 01 · 需求解析</span>
                  <span class="pillar-name">需求智能提炼</span>
                </div>
                <div class="pillar-desc">
                  长篇 PRD 一键梳理业务目标与测试边界；AI 语义解析自动提取核心业务流转与关键约束，分析结果直接回填且支持人工自由微调。
                </div>
                <div class="pillar-tags">
                  <span class="pillar-tag">长篇 PRD 语义解析</span>
                  <span class="pillar-tag">业务目标自动提炼</span>
                  <span class="pillar-tag">测试边界清晰划定</span>
                  <span class="pillar-tag">三档粒度策略</span>
                </div>
              </div>

              <!-- 支柱 2 -->
              <div class="pillar-card">
                <div class="pillar-top">
                  <span class="pillar-badge">支柱 02 · 流程把关</span>
                  <span class="pillar-name">人工前置把关</span>
                </div>
                <div class="pillar-desc">
                  测试点清单先确认再生成，杜绝盲目幻觉；在正式输出用例前输出结构化检查点矩阵，资深测试专家审查把关后再触发用例生成。
                </div>
                <div class="pillar-tags">
                  <span class="pillar-tag">结构化测试点 Checkpoint</span>
                  <span class="pillar-tag">人工审查前置把关</span>
                  <span class="pillar-tag">杜绝盲目幻觉扩散</span>
                  <span class="pillar-tag">多维正反覆盖</span>
                </div>
              </div>

              <!-- 支柱 3 -->
              <div class="pillar-card">
                <div class="pillar-top">
                  <span class="pillar-badge">支柱 03 · 资产工程</span>
                  <span class="pillar-name">Prompt 集中治理</span>
                </div>
                <div class="pillar-desc">
                  提示词集中版本化演进与任务引用防误删锁定；已被任务引用的提示词版本自动置为安全保护状态，保障用例生成历史完全可重现。
                </div>
                <div class="pillar-tags">
                  <span class="pillar-tag">提示词集中多版本管理</span>
                  <span class="pillar-tag">任务引用自动锁定</span>
                  <span class="pillar-tag">工程化防误删保护</span>
                  <span class="pillar-tag">生成质量稳定复现</span>
                </div>
              </div>

              <!-- 支柱 4 -->
              <div class="pillar-card">
                <div class="pillar-top">
                  <span class="pillar-badge">支柱 04 · 规范依据</span>
                  <span class="pillar-name">知识全链路可溯</span>
                </div>
                <div class="pillar-desc">
                  每条测试断言均有领域规章制度与 Wiki 条款精准支撑；需求、测试点、用例草稿与知识库段落双向穿透联动，满足严苛合规审计。
                </div>
                <div class="pillar-tags">
                  <span class="pillar-tag">规章切片与 Wiki 原文</span>
                  <span class="pillar-tag">断言双向穿透溯源</span>
                  <span class="pillar-tag">生成依据透明可信</span>
                  <span class="pillar-tag">企业级合规审计留痕</span>
                </div>
              </div>
            </div>

            <div class="summary-bottom-actions">
              <button class="bottom-btn" onclick="goToSlide(0)">
                <span>❮ 返回首页封面</span>
              </button>
              <div style="display: flex; gap: 10px;">
                <button class="bottom-btn" onclick="goToSlide(1)">
                  <span>⟳ 重新观看演示</span>
                </button>
                <button class="bottom-btn primary" onclick="toggleFullscreen()">
                  <span>⛶ 切换全屏</span>
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>

  </main>

  <!-- 底部浮动控制栏 -->
  <footer class="bottom-bar">
    <button class="bottom-btn" onclick="toggleDrawer()" title="目录大纲 (M)">
      <span>田</span>
      <span>目录</span>
    </button>
    <div class="bar-divider"></div>
    <button class="bottom-btn" onclick="prevSlide()" title="上一页 (←)">
      <span>❮ 上一页</span>
    </button>
    <div class="slide-counter" id="slideCounter">01 / 08</div>
    <button class="bottom-btn" onclick="nextSlide()" title="下一页 (→)">
      <span>下一页 ❯</span>
    </button>
    <div class="bar-divider"></div>
    <button class="bottom-btn" onclick="toggleFullscreen()" title="切换全屏 (F)">
      <span>⛶ 全屏</span>
    </button>
  </footer>

  <!-- Lightbox 大图查看模态窗 -->
  <div class="lightbox-modal" id="lightboxModal" onclick="handleLightboxBackdrop(event)">
    <div class="lightbox-topbar">
      <div class="lightbox-title">
        <span>🔍</span>
        <span id="lightboxTitle">高清真实截图预览</span>
      </div>
      <div class="lightbox-hint">快捷提示：按 ESC 或点击右上角关闭</div>
      <div class="lightbox-controls">
        <button class="lightbox-btn close" onclick="closeLightbox()">✕ 关闭预览</button>
      </div>
    </div>
    <div class="lightbox-body">
      <img id="lightboxImg" src="" alt="预览大图" />
    </div>
  </div>

  <!-- 页面导航抽屉 -->
  <div class="drawer-overlay" id="drawerOverlay" onclick="toggleDrawer()"></div>
  <div class="nav-drawer" id="navDrawer">
    <div class="drawer-header">
      <div class="drawer-title">
        <span>📑</span>
        <span>演示文稿页面导航 (点击直接直达对应功能页)</span>
      </div>
      <button class="bottom-btn" onclick="toggleDrawer()">✕ 关闭</button>
    </div>
    <div class="drawer-grid">
      <div class="drawer-card" onclick="goToSlideAndClose(0)">
        <span class="drawer-card-num">01</span>
        <span class="drawer-card-title">封面 · CaseGen</span>
      </div>
      <div class="drawer-card" onclick="goToSlideAndClose(1)">
        <span class="drawer-card-num">02</span>
        <span class="drawer-card-title">Wiki 知识库构建</span>
      </div>
      <div class="drawer-card" onclick="goToSlideAndClose(2)">
        <span class="drawer-card-num">03</span>
        <span class="drawer-card-title">外部知识库接入</span>
      </div>
      <div class="drawer-card" onclick="goToSlideAndClose(3)">
        <span class="drawer-card-num">04</span>
        <span class="drawer-card-title">智能需求分析</span>
      </div>
      <div class="drawer-card" onclick="goToSlideAndClose(4)">
        <span class="drawer-card-num">05</span>
        <span class="drawer-card-title">用例生成流水线</span>
      </div>
      <div class="drawer-card" onclick="goToSlideAndClose(5)">
        <span class="drawer-card-num">06</span>
        <span class="drawer-card-title">用例管理与交付</span>
      </div>
      <div class="drawer-card" onclick="goToSlideAndClose(6)">
        <span class="drawer-card-num">07</span>
        <span class="drawer-card-title">提示词管理</span>
      </div>
      <div class="drawer-card" onclick="goToSlideAndClose(7)">
        <span class="drawer-card-num">08</span>
        <span class="drawer-card-title">总结与核心价值</span>
      </div>
    </div>
  </div>

  <script>
    let currentSlide = 0;
    const totalSlides = 8;
    const slides = document.querySelectorAll('.slide');
    const progressBar = document.getElementById('progressBar');
    const slideCounter = document.getElementById('slideCounter');
    const drawerCards = document.querySelectorAll('.drawer-card');

    function updateSlide() {{
      slides.forEach((slide, idx) => {{
        slide.classList.toggle('active', idx === currentSlide);
      }});

      const percent = ((currentSlide + 1) / totalSlides) * 100;
      progressBar.style.width = percent + '%';

      const pageStr = String(currentSlide + 1).padStart(2, '0');
      const totalStr = String(totalSlides).padStart(2, '0');
      slideCounter.textContent = `${{pageStr}} / ${{totalStr}}`;

      drawerCards.forEach((card, idx) => {{
        card.classList.toggle('current', idx === currentSlide);
      }});
    }}

    function nextSlide() {{
      if (currentSlide < totalSlides - 1) {{
        currentSlide++;
        updateSlide();
      }}
    }}

    function prevSlide() {{
      if (currentSlide > 0) {{
        currentSlide--;
        updateSlide();
      }}
    }}

    function goToSlide(index) {{
      if (index >= 0 && index < totalSlides) {{
        currentSlide = index;
        updateSlide();
      }}
    }}

    function goToSlideAndClose(index) {{
      goToSlide(index);
      toggleDrawer();
    }}

    function toggleFullscreen() {{
      if (!document.fullscreenElement) {{
        document.documentElement.requestFullscreen().catch(err => {{
          console.warn('Fullscreen request failed:', err);
        }});
      }} else {{
        if (document.exitFullscreen) {{
          document.exitFullscreen();
        }}
      }}
    }}

    function toggleDrawer() {{
      const drawer = document.getElementById('navDrawer');
      const overlay = document.getElementById('drawerOverlay');
      const isActive = drawer.classList.contains('active');
      drawer.classList.toggle('active', !isActive);
      overlay.classList.toggle('active', !isActive);
    }}

    function switchWindowTab(btn, targetPaneId) {{
      const tabsContainer = btn.parentElement;
      tabsContainer.querySelectorAll('.window-tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');

      const showcaseWindow = btn.closest('.showcase-window');
      showcaseWindow.querySelectorAll('.window-pane').forEach(pane => pane.classList.remove('active'));

      const targetPane = document.getElementById(targetPaneId);
      if (targetPane) {{
        targetPane.classList.add('active');
      }}
    }}

    function openLightbox(imgIdOrSrc, title) {{
      const modal = document.getElementById('lightboxModal');
      const modalImg = document.getElementById('lightboxImg');
      const modalTitle = document.getElementById('lightboxTitle');

      if (imgIdOrSrc.startsWith('data:image') || imgIdOrSrc.startsWith('http')) {{
        modalImg.src = imgIdOrSrc;
      }} else {{
        const el = document.getElementById(imgIdOrSrc);
        modalImg.src = el ? el.src : '';
      }}

      modalTitle.textContent = title || '系统页面高清大图预览';
      modal.classList.add('active');
    }}

    function openCurrentWindowTabLightbox(bodyId) {{
      const bodyEl = document.getElementById(bodyId);
      if (!bodyEl) return;
      const activePane = bodyEl.querySelector('.window-pane.active') || bodyEl.querySelector('.window-pane');
      if (activePane) {{
        const img = activePane.querySelector('img');
        const title = activePane.getAttribute('data-title') || (img ? img.alt : '') || '高清真实截图预览';
        if (img && img.src) {{
          openLightbox(img.src, title);
        }}
      }}
    }}

    function closeLightbox() {{
      const modal = document.getElementById('lightboxModal');
      modal.classList.remove('active');
    }}

    function handleLightboxBackdrop(e) {{
      if (e.target.id === 'lightboxModal' || e.target.classList.contains('lightbox-body')) {{
        closeLightbox();
      }}
    }}

    window.addEventListener('keydown', (e) => {{
      const lightbox = document.getElementById('lightboxModal');
      const isLightboxOpen = lightbox.classList.contains('active');
      const drawer = document.getElementById('navDrawer');
      const isDrawerOpen = drawer.classList.contains('active');

      if (e.key === 'Escape') {{
        if (isLightboxOpen) {{
          closeLightbox();
          e.preventDefault();
          return;
        }}
        if (isDrawerOpen) {{
          toggleDrawer();
          e.preventDefault();
          return;
        }}
      }}

      if (isLightboxOpen) return;

      if (e.key === 'ArrowRight' || e.key === 'ArrowDown' || e.key === ' ' || e.key === 'PageDown') {{
        nextSlide();
        e.preventDefault();
      }} else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp' || e.key === 'PageUp') {{
        prevSlide();
        e.preventDefault();
      }} else if (e.key === 'Home') {{
        goToSlide(0);
        e.preventDefault();
      }} else if (e.key === 'End') {{
        goToSlide(totalSlides - 1);
        e.preventDefault();
      }} else if (e.key === 'f' || e.key === 'F') {{
        toggleFullscreen();
        e.preventDefault();
      }} else if (e.key >= '1' && e.key <= '8') {{
        goToSlide(parseInt(e.key) - 1);
        e.preventDefault();
      }} else if (e.key === 'm' || e.key === 'M') {{
        toggleDrawer();
        e.preventDefault();
      }}
    }});

    updateSlide();
  </script>
</body>
</html>
"""

    for path in OUT_PATHS:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"Successfully generated HTML presentation: {path} (size: {os.path.getsize(path)} bytes)")

if __name__ == "__main__":
    main()
