export type Process = 'bom' | 'cut' | 'bend' | 'drill' | 'weld' | 'finish'
export type DemoPage = 'overview' | 'engineering' | 'knowledge' | 'review'
export type PartId = 'body' | 'lid' | 'bracket' | 'plate'
export type Feature = { id: string; label: string; value: string; detail: string }
export type EngineeringPart = {
  id: PartId; code: string; name: string; revision: string; thickness: number; perSet: number
  formed: string; flat: string; description: string; processes: Process[]
}
export const PARTS: EngineeringPart[] = [
  { id: 'body', code: 'HY-2401-01', name: '机箱盒体', revision: 'A', thickness: 1.5, perSet: 1, formed: '320 × 220 × 80', flat: '480 × 380', description: '四边折弯 · 四角焊接 · 外表面拉丝', processes: ['cut', 'bend', 'weld', 'finish'] },
  { id: 'lid', code: 'HY-2401-02', name: '上盖板', revision: 'A', thickness: 1.5, perSet: 1, formed: '330 × 230', flat: '330 × 230', description: '平板切割 · 4 个激光孔 · 去毛刺', processes: ['cut', 'finish'] },
  { id: 'bracket', code: 'HY-2401-03', name: 'L 形安装支架', revision: 'A', thickness: 2, perSet: 2, formed: '80 × 80 × 40', flat: '120 × 80', description: '一道折弯 · 2 个孔后续钻至示例尺寸', processes: ['cut', 'bend', 'drill'] },
  { id: 'plate', code: 'HY-2401-04', name: '加强连接片', revision: 'A', thickness: 2, perSet: 2, formed: '100 × 40', flat: '100 × 40', description: '平板切割 · 2 个钻孔 · 去毛刺', processes: ['cut', 'drill', 'finish'] },
]
export const PROCESSES: { id: Process; label: string; short: string }[] = [
  { id: 'bom', label: '物料图表', short: 'BOM' }, { id: 'cut', label: '激光切割', short: 'CUT' },
  { id: 'bend', label: '数控折弯', short: 'BEND' }, { id: 'drill', label: '钻孔加工', short: 'DRILL' },
  { id: 'weld', label: '焊接作业', short: 'WELD' }, { id: 'finish', label: '打磨 / 抛光', short: 'FINISH' },
]
export const SAMPLE_FILES = ['机箱总装示例.dwg', '盒体展开示例.dxf', '上盖板示例.dxf', '安装支架示例.dxf', '连接片示例.dxf']
export const processName = (process: Process) => PROCESSES.find(row => row.id === process)!.label
export const partQuantity = (part: EngineeringPart, sets: number) => part.perSet * sets
export function featuresFor(part: EngineeringPart, process: Process, angle: number): Feature[] {
  if (process === 'cut') return [
    { id: 'outline', label: '外轮廓', value: part.flat + ' mm', detail: part.id === 'body' ? '示意展开，尚未应用折弯补偿' : '示例切割外轮廓' },
    ...(part.id === 'lid' ? [{ id: 'laser-holes', label: '激光内孔', value: '4 × Ø4.5', detail: '随轮廓切割，不另列钻孔工序' }] : []),
    ...(part.id === 'bracket' ? [{ id: 'pilot-holes', label: '激光预孔', value: '2 × Ø5.0', detail: 'H01 / H02 后续钻至 Ø6.5，引用相同孔特征' }] : []),
    { id: 'material', label: '板材规格', value: `SS304 · ${part.thickness} mm`, detail: '材料与板厚均为模拟假设' },
  ]
  if (process === 'bend') return Array.from({ length: part.id === 'body' ? 4 : 1 }, (_, i) => ({ id: 'B0' + (i + 1), label: 'B0' + (i + 1) + ' 折弯线', value: `${angle}° · 向上`, detail: i === 0 ? '成品内夹角；相对图示基准面' : '示例顺序，实际需结合工厂模具校核' }))
  if (process === 'drill') return ['H01', 'H02'].map((id, i) => ({ id, label: id + ' 通孔', value: part.id === 'bracket' ? 'Ø6.5 · 通孔' : 'Ø5.5 · 通孔', detail: `距图示左基准 ${part.id === 'bracket' ? 60 + i * 40 : 20 + i * 60} mm；板厚 ${part.thickness} mm` }))
  if (process === 'weld') return ['W01', 'W02', 'W03', 'W04'].map((id, i) => ({ id, label: id + ' 角部接头', value: '连续焊 · 示例', detail: i === 3 ? '背面位置以虚线表示；焊接尺寸待确认' : '图示角部位置；实际工艺由工厂确定' }))
  if (process === 'finish') return [{ id: 'surface', label: 'S01 处理区域', value: part.id === 'body' ? '外表面拉丝' : '切割边去毛刺', detail: '示例作业要求；磨料、粗糙度及工时待工厂校准' }]
  return []
}
export function documentCount() { return 1 + PARTS.reduce((sum, part) => sum + part.processes.length, 0) }
export function demoIssues(kFactor: string, weldSize: string) {
  const k = Number(kFactor), weld = Number(weldSize)
  return [
    ...(!kFactor.trim() || !Number.isFinite(k) || k <= 0 || k >= 1 ? [{ id: 'bend-basis', part: 'body' as PartId, process: 'bend' as Process, title: '盒体展开补偿未确认', detail: '演示 K 因子留空，激光展开仅作参考示意。' }] : []),
    ...(!weldSize.trim() || !Number.isFinite(weld) || weld <= 0 || weld > 10 ? [{ id: 'weld-size', part: 'body' as PartId, process: 'weld' as Process, title: '角部焊接尺寸待确认', detail: '焊接位置已示意，焊接尺寸和适用工艺仍需确认。' }] : []),
  ]
}
export function bomCsv(sets: number) {
  const rows = [['DEMO：模拟物料清单，非生产依据'], ['图号', '零件', '版本', '材料', '板厚mm', '单套用量', '模拟套数', '总数量', '成形尺寸mm'], ...PARTS.map(p => [p.code, p.name, p.revision, 'SS304', String(p.thickness), String(p.perSet), String(sets), String(partQuantity(p, sets)), p.formed])]
  return '\uFEFF' + rows.map(row => row.map(value => '"' + value.replaceAll('"', '""') + '"').join(',')).join('\r\n')
}
export const RULES = [
  { id: 'R-CUT-01', process: 'cut' as Process, title: '切割线与辅助线分层', text: '切割外轮廓与内孔单独显示；折弯线、中心线和文字作为参考层。', basis: '演示工艺模板 · 待工厂确认' },
  { id: 'R-BEND-01', process: 'bend' as Process, title: '折弯方向与角度口径', text: '以选定基准面说明向上/向下，角度采用成品内夹角；展开补偿单独记录。', basis: '演示工艺模板 · 待工厂确认' },
  { id: 'R-DRILL-01', process: 'drill' as Process, title: '预孔与钻孔共享特征编号', text: '激光预孔和后续钻孔引用同一个孔，分开描述工步，不重复计算物理孔数量。', basis: '演示工艺模板 · 待工厂确认' },
  { id: 'R-WELD-01', process: 'weld' as Process, title: '焊接位置与工艺依据分别确认', text: '图上定位接头，焊接方法、尺寸及适用规程由工厂补充。本例不是已批准 WPS。', basis: '演示案例 · 非企业标准' },
  { id: 'R-FINISH-01', process: 'finish' as Process, title: '表面处理指定范围', text: '区分外观面、焊缝修整和切割边去毛刺，分别记录作业要求及计时口径。', basis: '演示工艺模板 · 待工厂确认' },
]
export function demoTimes(sets: number) {
  return [
    { name: '激光切割', basis: `每件 0.8 min × ${6 * sets} 件`, setup: 12, work: 0.8 * 6 * sets },
    { name: '数控折弯', basis: `每道 0.25 min × ${6 * sets} 道`, setup: 10, work: 0.25 * 6 * sets },
    { name: '钻孔加工', basis: `每孔 0.2 min × ${8 * sets} 孔`, setup: 8, work: 0.2 * 8 * sets },
    { name: '焊接作业', basis: `每个盒体 3 min × ${sets} 件`, setup: 15, work: 3 * sets },
    { name: '打磨 / 抛光', basis: `每个盒体 2 min × ${sets} 件`, setup: 5, work: 2 * sets },
  ]
}
