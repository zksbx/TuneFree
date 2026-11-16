/**
 * TuneFree - QQ音乐 mflac / mgg 解密钩子
 *
 * 由 main.py 通过 Frida 注入到 QQMusic.exe，对外暴露 rpc.exports.decrypt(源文件, 输出文件)。
 *
 * 与旧版的差别：所有导出符号都做存在性校验，打开文件 / 取大小 / 读数据都检查返回值。
 * 这样 QQ音乐更新导致符号失效时，报出来的是「版本不匹配」这种能看懂的原因，
 * 而不是以前那句谁也看不懂的 invalid size。
 */
const TARGET_DLL = "QQMusicCommon.dll";

/* MSVC 修饰名。QQ音乐换编译器 / 改类定义都会变，这里是唯一需要跟着更新的地方。 */
var SYMBOLS = {
  ctor: "??0EncAndDesMediaFile@@QAE@XZ",
  dtor: "??1EncAndDesMediaFile@@QAE@XZ",
  open: "?Open@EncAndDesMediaFile@@QAE_NPB_W_N1@Z",
  getSize: "?GetSize@EncAndDesMediaFile@@QAEKXZ",
  read: "?Read@EncAndDesMediaFile@@QAEKPAEK_J@Z",
};

var BOUND = null;
var OBJECT_SIZE = 0x28;          // EncAndDesMediaFile 的实例大小，按当前版本实测
var MAX_FILE_SIZE = 512 * 1024 * 1024;
var READ_CHUNK = 4 * 1024 * 1024;  // 单次 Read 的上限，分块循环读到末尾

/** 把 5 个符号全部找出来并绑定成 NativeFunction，只在第一次调用时做。 */
function bindSymbols() {
  if (BOUND !== null) {
    return BOUND;
  }

  var missing = [];
  var addr = {};
  for (var key in SYMBOLS) {
    var a = Module.findExportByName(TARGET_DLL, SYMBOLS[key]);
    if (a === null) {
      missing.push(SYMBOLS[key]);
    }
    addr[key] = a;
  }

  if (missing.length > 0) {
    throw new Error(
      "QQ音乐版本不匹配：在 " +
        TARGET_DLL +
        " 里找不到 " +
        missing.length +
        " 个导出符号（" +
        missing.join("、") +
        "）。请把 QQ音乐降级到 TuneFree 适配的版本，或等 TuneFree 更新。"
    );
  }

  BOUND = {
    ctor: new NativeFunction(addr.ctor, "pointer", ["pointer"], "thiscall"),
    dtor: new NativeFunction(addr.dtor, "void", ["pointer"], "thiscall"),
    open: new NativeFunction(addr.open, "bool", ["pointer", "pointer", "bool", "bool"], "thiscall"),
    getSize: new NativeFunction(addr.getSize, "uint32", ["pointer"], "thiscall"),
    read: new NativeFunction(addr.read, "uint", ["pointer", "pointer", "uint32", "uint64"], "thiscall"),
  };
  return BOUND;
}

/** 启动时先绑定一次符号，符号缺失就在控制台留一行线索（不影响后续逐首解密）。 */
(function () {
  try {
    bindSymbols();
  } catch (e) {
    console.error("[TuneFree] " + e.message);
  }
})();

/* ---------- 直接写盘 ----------
 * 老做法是把整首歌 readByteArray 拷到 Frida 侧，再让 Frida 写文件：
 * 几十 MB 要跨进程搬一次，写文件的是注入方，杀软和「慢」都出在这。
 * 这里改成在 QQ 音乐自己的进程里调 kernel32 写盘，数据不出进程。
 */
var GENERIC_WRITE = 0x40000000;
var CREATE_ALWAYS = 2;
var FILE_ATTRIBUTE_NORMAL = 0x80;
var WRITE_CHUNK = 4 * 1024 * 1024;
var K32 = null;
var FREE = null;

function bindKernel32() {
  if (K32 !== null) {
    return K32;
  }
  var create = Module.findExportByName("kernel32.dll", "CreateFileW");
  var write = Module.findExportByName("kernel32.dll", "WriteFile");
  var close = Module.findExportByName("kernel32.dll", "CloseHandle");
  if (create === null || write === null || close === null) {
    throw new Error("找不到 kernel32 的写入接口，退回 Frida 写盘");
  }
  K32 = {
    create: new NativeFunction(create, "pointer",
      ["pointer", "uint32", "uint32", "pointer", "uint32", "uint32", "pointer"]),
    write: new NativeFunction(write, "bool", ["pointer", "pointer", "uint32", "pointer", "pointer"]),
    close: new NativeFunction(close, "bool", ["pointer"]),
  };
  return K32;
}

/** 把目标进程里的这块内存直接写成文件，返回写入字节数。 */
function writeInProcess(buffer, size, path) {
  var k = bindKernel32();
  var wide = Memory.allocUtf16String(path);
  var handle = k.create(wide, GENERIC_WRITE, 0, ptr(0), CREATE_ALWAYS,
                        FILE_ATTRIBUTE_NORMAL, ptr(0));
  if (handle.equals(ptr("-1"))) {
    throw new Error("QQ音乐写不了这个位置：" + path);
  }
  var done = Memory.alloc(4);
  var offset = 0;
  try {
    while (offset < size) {
      var want = Math.min(size - offset, WRITE_CHUNK);
      if (!k.write(handle, buffer.add(offset), want, done, ptr(0))) {
        throw new Error("写文件中途失败：" + path);
      }
      offset += want;
    }
  } finally {
    k.close(handle);
  }
  return offset;
}

/** 用完把 Memory.alloc 的缓冲还回去，否则连续解密几十首会把 QQ音乐内存撑大。 */
function releaseBuffer(buffer) {
  if (FREE === null) {
    var addr = Module.findExportByName(null, "free");
    FREE = addr === null ? false : new NativeFunction(addr, "void", ["pointer"]);
  }
  if (FREE !== false) {
    try {
      FREE(buffer);
    } catch (e) {
      /* 还不了就算了，别影响已经写好的文件 */
    }
  }
}

/** 解密到目标进程的一块内存里，返回 {buffer, size}。 */
function decryptToBuffer(srcFileName) {
  var fns = bindSymbols();

  var obj = Memory.alloc(OBJECT_SIZE);
  fns.ctor(obj);

  var buf = null;
  try {
    var nameUtf16 = Memory.allocUtf16String(srcFileName);
    var opened = fns.open(obj, nameUtf16, 1, 0);
    if (!opened) {
      throw new Error(
        "打不开这个文件，可能还没下载完整，或者需要先在 QQ音乐里播放一遍再试：" + srcFileName
      );
    }

    var size = fns.getSize(obj);
    if (!size || size < 4) {
      throw new Error("读到的文件大小为 " + size + " 字节，文件可能已损坏：" + srcFileName);
    }
    if (size > MAX_FILE_SIZE) {
      throw new Error("文件过大（" + size + " 字节），超出 TuneFree 的处理范围：" + srcFileName);
    }

    buf = Memory.alloc(size);
    var offset = 0;
    // 分块读，避免某些版本一次性返回不足量时把音频截断
    while (offset < size) {
      var want = Math.min(size - offset, READ_CHUNK);
      var got = fns.read(obj, buf.add(offset), want, offset);
      if (!got) {
        break;
      }
      offset += got;
    }
    if (!offset) {
      throw new Error("解密读取返回 0 字节：" + srcFileName);
    }
    return { buffer: buf, size: offset };
  } catch (e) {
    if (buf !== null) {
      releaseBuffer(buf);
    }
    throw e;
  } finally {
    fns.dtor(obj);
  }
}

rpc.exports = {
  /**
   * 解密单个文件，并让 QQ音乐自己把结果写到 dstFileName。
   * 数据不经过 Frida，几十 MB 的跨进程拷贝和「注入方写盘」都省了。
   * @param {string} srcFileName 加密文件路径（.mflac / .mgg）
   * @param {string} dstFileName 解密结果写到哪（可以直接是中文名）
   * @returns {number} 实际写出的字节数
   */
  decrypt: function (srcFileName, dstFileName) {
    var part = decryptToBuffer(srcFileName);
    try {
      return writeInProcess(part.buffer, part.size, dstFileName);
    } finally {
      releaseBuffer(part.buffer);
    }
  },

  /**
   * 老写法：解密结果先读回 Frida 再写文件。
   * 只在 decrypt 写不进去时兜底（目标目录权限特殊、或者拿不到 kernel32）。
   */
  decryptfile: function (srcFileName, tmpFileName) {
    var part = decryptToBuffer(srcFileName);
    var data = null;
    try {
      data = part.buffer.readByteArray(part.size);
    } finally {
      releaseBuffer(part.buffer);
    }
    var tmpFile = new File(tmpFileName, "wb");
    try {
      tmpFile.write(data);
    } finally {
      tmpFile.flush();
      tmpFile.close();
    }
    return part.size;
  },

  /**
   * 只解密文件开头的一段，用于列表快速读取封面 / 时长，不必整首解密。
   * @param {string} srcFileName 加密文件路径
   * @param {number} limit 最多读多少字节
   * @returns {ArrayBuffer} 解密后的字节
   */
  readhead: function (srcFileName, limit) {
    var fns = bindSymbols();
    var obj = Memory.alloc(OBJECT_SIZE);
    fns.ctor(obj);
    try {
      var nameUtf16 = Memory.allocUtf16String(srcFileName);
      if (!fns.open(obj, nameUtf16, 1, 0)) {
        throw new Error("打不开这个文件：" + srcFileName);
      }
      var size = fns.getSize(obj);
      if (!size) {
        throw new Error("读到的文件大小为 0：" + srcFileName);
      }
      var want = Math.min(size, limit || 262144);
      var buf = Memory.alloc(want);
      var got = fns.read(obj, buf, want, 0);
      if (!got) {
        throw new Error("读取文件头返回 0 字节：" + srcFileName);
      }
      return buf.readByteArray(got);
    } finally {
      fns.dtor(obj);
    }
  },

  /**
   * 只解密文件末尾的一段，用于 Ogg 尾部 granule 推算时长。
   * @returns {ArrayBuffer} 解密后的字节
   */
  readtail: function (srcFileName, limit) {
    var fns = bindSymbols();
    var obj = Memory.alloc(OBJECT_SIZE);
    fns.ctor(obj);
    try {
      var nameUtf16 = Memory.allocUtf16String(srcFileName);
      if (!fns.open(obj, nameUtf16, 1, 0)) {
        throw new Error("打不开这个文件：" + srcFileName);
      }
      var size = fns.getSize(obj);
      if (!size) {
        throw new Error("读到的文件大小为 0：" + srcFileName);
      }
      var want = Math.min(size, limit || 4096);
      var offset = size - want;
      var buf = Memory.alloc(want);
      var got = fns.read(obj, buf, want, offset);
      if (!got) {
        throw new Error("读取文件尾返回 0 字节：" + srcFileName);
      }
      return buf.readByteArray(got);
    } finally {
      fns.dtor(obj);
    }
  },

  /** 供 main.py 做启动自检：能定位符号就返回 true，否则返回缺失清单。 */
  probe: function () {
    try {
      bindSymbols();
      return { ok: true, missing: [] };
    } catch (e) {
      var m = e.message;
      return { ok: false, message: m };
    }
  },
};

