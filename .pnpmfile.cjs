module.exports = {
  hooks: {
    readPackage(pkg) {
      // pnpm 12.15 infers Windows-only support from this cross-platform package's name.
      if (pkg.name === 'get-windows' && pkg.version === '9.3.0' && pkg.os === undefined) {
        pkg.os = ['darwin', 'linux', 'win32']
      }
      return pkg
    }
  }
}
