/**
 * 最小 ZIP 打包（单个文件、不压缩的 STORE 方式）。用于演示页：预览环境的下载接口
 * 只接受部分扩展名（txt/json/csv/zip 等），音频与字幕打成 zip 后再交给它。
 */
const CRC_TABLE = (() => {
  const table = new Uint32Array(256)
  for (let n = 0; n < 256; n += 1) {
    let c = n
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    table[n] = c >>> 0
  }
  return table
})()

export function crc32(data: Uint8Array): number {
  let crc = 0xffffffff
  for (let i = 0; i < data.length; i += 1) crc = CRC_TABLE[(crc ^ data[i]) & 0xff] ^ (crc >>> 8)
  return (crc ^ 0xffffffff) >>> 0
}

export function zipSingleFile(filename: string, data: Uint8Array): Uint8Array {
  const name = new TextEncoder().encode(filename)
  const crc = crc32(data)
  const local = new Uint8Array(30 + name.length)
  const lv = new DataView(local.buffer)
  lv.setUint32(0, 0x04034b50, true)  // 本地文件头
  lv.setUint16(4, 20, true)          // 需要的版本
  lv.setUint16(6, 0x0800, true)      // 文件名为 UTF-8
  lv.setUint16(8, 0, true)           // STORE，不压缩
  lv.setUint32(14, crc, true)
  lv.setUint32(18, data.length, true)
  lv.setUint32(22, data.length, true)
  lv.setUint16(26, name.length, true)
  local.set(name, 30)
  const central = new Uint8Array(46 + name.length)
  const cv = new DataView(central.buffer)
  cv.setUint32(0, 0x02014b50, true)  // 中央目录
  cv.setUint16(4, 20, true)
  cv.setUint16(6, 20, true)
  cv.setUint16(8, 0x0800, true)
  cv.setUint32(16, crc, true)
  cv.setUint32(20, data.length, true)
  cv.setUint32(24, data.length, true)
  cv.setUint16(28, name.length, true)
  central.set(name, 46)
  const end = new Uint8Array(22)
  const ev = new DataView(end.buffer)
  ev.setUint32(0, 0x06054b50, true)  // 目录结束记录
  ev.setUint16(8, 1, true)
  ev.setUint16(10, 1, true)
  ev.setUint32(12, central.length, true)
  ev.setUint32(16, local.length + data.length, true)
  const out = new Uint8Array(local.length + data.length + central.length + end.length)
  out.set(local, 0)
  out.set(data, local.length)
  out.set(central, local.length + data.length)
  out.set(end, local.length + data.length + central.length)
  return out
}
