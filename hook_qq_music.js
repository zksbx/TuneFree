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

rpc.exports = {
  /**
   * 解密单个文件。
   * @param {string} srcFileName 加密文件路径（.mflac / .mgg）
   * @param {string} tmpFileName 解密结果写到哪里
   * @returns {number} 实际写出的字节数
   */
  decrypt: function (srcFileName, tmpFileName) {
    var fns = bindSymbols();

    var obj = Memory.alloc(OBJECT_SIZE);
    fns.ctor(obj);

    var ok = false;
    var size = 0;
    var written = 0;
    var data = null;

    try {
      var nameUtf16 = Memory.allocUtf16String(srcFileName);
      var opened = fns.open(obj, nameUtf16, 1, 0);
      if (!opened) {
        throw new Error(
          "打不开这个文件，可能还没下载完整，或者需要先在 QQ音乐里播放一遍再试：" + srcFileName
        );
      }

      size = fns.getSize(obj);
      if (!size || size < 4) {
        throw new Error("读到的文件大小为 " + size + " 字节，文件可能已损坏：" + srcFileName);
      }
      if (size > MAX_FILE_SIZE) {
        throw new Error("文件过大（" + size + " 字节），超出 TuneFree 的处理范围：" + srcFileName);
      }

      var buf = Memory.alloc(size);
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
      written = offset;
      if (!written) {
        throw new Error("解密读取返回 0 字节：" + srcFileName);
      }
      data = buf.readByteArray(written);
      ok = true;
    } finally {
      fns.dtor(obj);
    }

    if (!ok) {
      throw new Error("解密失败：" + srcFileName);
    }

    var tmpFile = new File(tmpFileName, "wb");
    try {
      tmpFile.write(data);
    } finally {
      tmpFile.flush();
      tmpFile.close();
    }
    return written;
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
