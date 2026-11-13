import { type EngineeringPart, type Process, processName } from './engineering-demo'

const escape = (value: string) => value.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' }[c]!))
type Options = { selected?: string; angle: number; structure?: boolean; showLabels?: boolean }
export function drawingSvg(part: EngineeringPart, process: Process, options: Options) {
  const selected = options.selected || ''
  const angle = Math.max(5, Math.min(175, Number.isFinite(options.angle) ? options.angle : 90))
  const stroke = (id: string, fallback = '#246a96') => selected === id ? '#eb8c34' : fallback
  const line = (x1: number, y1: number, x2: number, y2: number, color = '#7b92a3', dash = '') => `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${color}" stroke-width="1.3"${dash ? ` stroke-dasharray="${dash}"` : ''}/>`
  const text = (x: number, y: number, value: string, size = 13, color = '#506b80', anchor = 'middle') => `<text x="${x}" y="${y}" font-size="${size}" fill="${color}" text-anchor="${anchor}">${escape(value)}</text>`
  const dim = (x1: number, y1: number, x2: number, y2: number, value: string) => `<path d="M${x1},${y1}L${x2},${y2}" fill="none" stroke="#8da5b7" stroke-width="1" marker-start="url(#dimension-arrow)" marker-end="url(#dimension-arrow)"/>${text((x1 + x2) / 2 + (x1 === x2 ? -23 : 0), (y1 + y2) / 2 - 9, value)}`
  const dot = (x: number, y: number, id: string, label: string) => `<circle cx="${x}" cy="${y}" r="13" fill="${stroke(id, '#247eaf')}"/>${text(x, y + 4, label, 11, '#fff')}`
  let art = ''
  const holes = (points: [number, number][], radius: number, isDrill: boolean) => points.map(([x, y], index) => {
    const id = isDrill ? 'H0' + (index + 1) : part.id === 'lid' ? 'laser-holes' : 'pilot-holes'
    return `<circle cx="${x}" cy="${y}" r="${radius}" fill="${selected === id ? '#fff0dc' : '#fff'}" stroke="${stroke(id)}" stroke-width="${selected === id ? 3 : 1.7}"/>${line(x - 15, y, x + 15, y, '#a5bac7', '3 3')}${line(x, y - 15, x, y + 15, '#a5bac7', '3 3')}${isDrill ? dot(x + 35, y - 35, id, String(index + 1)) + text(x + 35, y - 56, id, 11) : ''}`
  }).join('')
  const bodyPath = 'M290 80H610V160H690V380H610V460H290V380H210V160H290Z'
  const bodyBends = [[290, 160, 610, 160], [610, 160, 610, 380], [290, 380, 610, 380], [290, 160, 290, 380]]
  if (process === 'weld' || process === 'finish' && part.id === 'body' || options.structure) {
    if (part.id === 'bracket') {
      art = `<path d="M280 170L450 100L450 260L620 330L450 400L280 330Z" fill="#eaf3f9" stroke="#306b91" stroke-width="2.4"/><path d="M280 330L450 260L620 330M450 260V100" fill="none" stroke="#75a2bf" stroke-width="2"/>${text(450, 470, `L 形结构示意 · 成品内夹角 ${angle}°`, 15)}`
    } else if (part.id === 'lid' || part.id === 'plate') {
      art = `<path d="M240 280L480 170L680 275L440 385Z" fill="#e7f2f8" stroke="#367a9f" stroke-width="2.5"/><path d="M240 280V290L440 395L680 285V275M440 385V395" fill="none" stroke="#367a9f" stroke-width="2"/>${text(450, 455, '平板结构示意 · 非制造模型', 15)}`
    } else {
      art = `<path d="M300 190L500 110L650 200L450 280Z" fill="#f6fafc" stroke="#507f9b" stroke-width="2"/><path d="M300 190L450 280V430L300 340Z" fill="${process === 'finish' ? '#d8eee5' : '#e1eef6'}" stroke="#507f9b" stroke-width="2"/><path d="M450 280L650 200V350L450 430Z" fill="${process === 'finish' ? '#c3e1d5' : '#cfe2ee'}" stroke="#507f9b" stroke-width="2"/><path d="M310 192L500 121L638 201L450 269Z" fill="#fff" stroke="#9dbbcd"/>${line(500, 121, 500, 250, '#a7bfce', '5 5')}`
      if (process === 'weld') {
        const seams = [[300, 190, 300, 340], [450, 280, 450, 430], [650, 200, 650, 350], [500, 110, 500, 250]]
        art += seams.map(([x1, y1, x2, y2], i) => `<path d="M${x1} ${y1}L${x2} ${y2}" stroke="${stroke('W0' + (i + 1), '#d97d43')}" stroke-width="${selected === 'W0' + (i + 1) ? 7 : 4}"${i === 3 ? ' stroke-dasharray="6 5"' : ''}/>${text(x1 + (i === 0 ? -45 : 36), (y1 + y2) / 2, 'W0' + (i + 1), 14, '#b86b37')}`).join('')
        art += text(450, 490, '四角接头位置示例 · 背面接头以虚线表示', 14)
      } else if (process === 'finish') art += `${text(450, 490, 'S01 外表面处理区域 · 示例拉丝方向', 14, '#3c8a6a')}${line(345, 240, 345, 325, '#55a07f')}${line(375, 258, 375, 342, '#55a07f')}${line(405, 276, 405, 360, '#55a07f')}`
      else art += text(450, 490, '盒体结构示意 · 不按比例 · 非制造模型', 14)
    }
  } else if (part.id === 'body') {
    art = `<path d="${bodyPath}" fill="#f2f8fc" stroke="${stroke('outline')}" stroke-width="${selected === 'outline' ? 3.8 : 2}"/>`
    if (process === 'bend') art += bodyBends.map(([x1, y1, x2, y2], i) => {
      const id = 'B0' + (i + 1)
      return `<path d="M${x1} ${y1}L${x2} ${y2}" fill="none" stroke="${stroke(id, '#926dc0')}" stroke-width="${selected === id ? 4 : 2}" stroke-dasharray="10 5"/>${text((x1 + x2) / 2 + (i === 1 ? -45 : i === 3 ? 45 : 0), (y1 + y2) / 2 + (i === 0 ? 24 : -12), `${id} ↑ ${angle}°`, 13, stroke(id, '#8763b3'))}`
    }).join('')
    else art += bodyBends.map(([x1, y1, x2, y2]) => line(x1, y1, x2, y2, '#b7c7d2', '5 5')).join('')
    art += dim(210, 493, 690, 493, '480') + dim(162, 80, 162, 460, '380') + text(450, 300, '基准面 A', 16, '#8fa9ba')
  } else if (part.id === 'lid') {
    art = `<rect x="285" y="145" width="330" height="230" rx="3" fill="#f3f8fb" stroke="${stroke('outline')}" stroke-width="2.2"/>${holes([[305, 165], [595, 165], [305, 355], [595, 355]], 4.5, false)}${dim(285, 415, 615, 415, '330')}${dim(240, 145, 240, 375, '230')}${text(450, 263, process === 'finish' ? 'S01 四周切割边去毛刺' : '4 × Ø4.5 激光切割孔', 14)}`
  } else if (part.id === 'bracket') {
    art = `<rect x="270" y="130" width="360" height="240" rx="4" fill="#f3f8fb" stroke="${stroke('outline')}" stroke-width="2.2"/>${holes([[450, 250], [570, 250]], process === 'drill' ? 9.75 : 7.5, process === 'drill')}${dim(270, 415, 630, 415, '120')}${dim(225, 130, 225, 370, '80')}`
    art += `<path d="M390 130V370" stroke="${stroke('B01', process === 'bend' ? '#926dc0' : '#bccbd5')}" stroke-width="${selected === 'B01' ? 4 : 1.8}" stroke-dasharray="10 5"/>`
    if (process === 'bend') art += text(390, 105, `B01 ↑ ${angle}° · 向上`, 14, '#8763b3')
    if (process === 'drill') art += dim(450, 330, 570, 330, '孔中心距 40') + text(450, 468, 'H01 / H02：激光预孔后钻至 Ø6.5 · 通孔', 14)
    else art += text(450, 468, '示例展开轮廓 · 折弯参考线单独显示', 14)
  } else {
    art = `<rect x="300" y="220" width="300" height="120" rx="4" fill="#f3f8fb" stroke="${stroke('outline')}" stroke-width="2.2"/>${process === 'cut' ? text(450, 285, '孔位由后续钻孔图标注', 13) : holes([[360, 280], [540, 280]], 8.25, process === 'drill')}${dim(300, 385, 600, 385, '100')}${dim(255, 220, 255, 340, '40')}${process === 'drill' ? dim(360, 195, 540, 195, '孔中心距 60') : ''}`
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 620" role="img" aria-label="${escape(part.name + ' ' + processName(process) + '模拟示意图')}" style="font-family:Arial,'Microsoft YaHei',sans-serif;background:white"><defs><marker id="dimension-arrow" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M10 0L0 5L10 10Z" fill="#8da5b7"/></marker><pattern id="engineering-grid" width="25" height="25" patternUnits="userSpaceOnUse"><path d="M25 0H0V25" fill="none" stroke="#edf3f7" stroke-width="0.6"/></pattern></defs><rect width="900" height="620" fill="white"/><rect x="20" y="20" width="860" height="580" fill="url(#engineering-grid)" stroke="#dce7ee"/><text x="40" y="46" font-size="11" fill="#95aaba">HY ENGINEERING / DEMO DRAWING</text>${art}<rect x="20" y="530" width="860" height="70" fill="white" stroke="#dce7ee"/><path d="M580 530V600M720 530V600M20 565H880" stroke="#dce7ee"/>${text(40, 553, part.name + ' / ' + processName(process), 15, '#24465f', 'start')}${text(40, 587, 'DEMO · 示例几何与工艺 · 非加工文件', 12, '#b67b40', 'start')}${text(600, 553, part.code, 12, '#56758c', 'start')}${text(600, 587, 'SS304 / ' + part.thickness + ' mm', 11, '#718b9f', 'start')}${text(740, 553, '版本 A / 单位 mm', 11, '#718b9f', 'start')}${text(740, 587, '示意，不按比例', 11, '#718b9f', 'start')}</svg>`
}
