using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("PrismFX Studio")]
[assembly: AssemblyProduct("PrismFX Studio")]
[assembly: AssemblyDescription("165 Hz GPU Windows 11 Desktop Motion & Window FX Studio")]
[assembly: AssemblyCompany("PrismFX Studio")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

namespace PrismFXLauncher
{
    internal static class Program
    {
        private const uint LOAD_LIBRARY_SEARCH_DEFAULT_DIRS = 0x00001000;

        private const FileSystemRights UnsafeWriteRights =
            FileSystemRights.WriteData |
            FileSystemRights.AppendData |
            FileSystemRights.CreateFiles |
            FileSystemRights.CreateDirectories |
            FileSystemRights.DeleteSubdirectoriesAndFiles |
            FileSystemRights.Delete |
            FileSystemRights.ChangePermissions |
            FileSystemRights.TakeOwnership;

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetDefaultDllDirectories(uint DirectoryFlags);

        [STAThread]
        private static int Main(string[] args)
        {
            try
            {
                SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
            }
            catch
            {
            }

            bool elevated = IsRunningElevated();
            string selfExe = Assembly.GetExecutingAssembly().Location;
            string baseDir = Path.GetDirectoryName(selfExe) ?? AppDomain.CurrentDomain.BaseDirectory;
            string mainPy = Path.Combine(baseDir, "main.py");

            if (!File.Exists(mainPy))
            {
                MessageBox.Show(
                    "PrismFX Studio could not locate main.py in:\n" + baseDir,
                    "PrismFX Studio",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
                return 2;
            }

            string pythonW = ResolvePythonW(baseDir, elevated);
            if (string.IsNullOrEmpty(pythonW))
            {
                string msg = elevated
                    ? "Python 3 (pythonw.exe) was not found in an administrator-protected system directory.\n" +
                      "When running elevated, PrismFX only uses system-wide Python installations (e.g. under Program Files)."
                    : "Python 3 (pythonw.exe) with PySide6 was not found on this machine.\n" +
                      "Please install Python 3.10+ or set PRISMFX_PYTHONW.";
                MessageBox.Show(
                    msg,
                    "PrismFX Studio",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
                return 3;
            }

            var argBuilder = new StringBuilder();
            argBuilder.Append(QuoteArg(mainPy));
            if (args != null)
            {
                foreach (string a in args)
                {
                    argBuilder.Append(' ');
                    argBuilder.Append(QuoteArg(a));
                }
            }

            var psi = new ProcessStartInfo
            {
                FileName = pythonW,
                Arguments = argBuilder.ToString(),
                WorkingDirectory = baseDir,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden
            };
            if (!elevated)
            {
                psi.EnvironmentVariables["PRISMFX_LAUNCHER_EXE"] = selfExe;
            }

            try
            {
                Process proc = Process.Start(psi);
                if (proc == null)
                {
                    return 4;
                }
                return 0;
            }
            catch (Exception ex)
            {
                MessageBox.Show(
                    "Failed to start PrismFX Studio:\n" + ex.Message,
                    "PrismFX Studio",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
                return 5;
            }
        }

        private static bool IsRunningElevated()
        {
            try
            {
                using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
                {
                    if (identity == null) return false;
                    WindowsPrincipal principal = new WindowsPrincipal(identity);
                    return principal.IsInRole(WindowsBuiltInRole.Administrator);
                }
            }
            catch
            {
                // Fail-closed: treat unknown elevation state as elevated so untrusted paths are skipped.
                return true;
            }
        }

        private static bool IsUnprivilegedSid(SecurityIdentifier sid)
        {
            if (sid == null) return true;
            string v = sid.Value;
            return string.Equals(v, "S-1-1-0", StringComparison.OrdinalIgnoreCase)
                || string.Equals(v, "S-1-5-4", StringComparison.OrdinalIgnoreCase)
                || string.Equals(v, "S-1-5-7", StringComparison.OrdinalIgnoreCase)
                || string.Equals(v, "S-1-5-11", StringComparison.OrdinalIgnoreCase)
                || string.Equals(v, "S-1-5-32-545", StringComparison.OrdinalIgnoreCase)
                || string.Equals(v, "S-1-15-2-1", StringComparison.OrdinalIgnoreCase)
                || string.Equals(v, "S-1-15-2-2", StringComparison.OrdinalIgnoreCase);
        }

        private static bool IsUnderProtectedRoot(string fullPath)
        {
            string trimmed = fullPath.TrimEnd('\\');
            string[] roots =
            {
                Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
                Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
                Environment.GetEnvironmentVariable("ProgramW6432"),
                Environment.GetFolderPath(Environment.SpecialFolder.Windows)
            };

            foreach (string rawRoot in roots)
            {
                if (string.IsNullOrEmpty(rawRoot)) continue;
                try
                {
                    string root = Path.GetFullPath(rawRoot).TrimEnd('\\');
                    if (string.Equals(trimmed, root, StringComparison.OrdinalIgnoreCase)
                        || trimmed.StartsWith(root + "\\", StringComparison.OrdinalIgnoreCase))
                    {
                        return true;
                    }
                }
                catch
                {
                }
            }
            return false;
        }

        private static bool IsAdminProtectedPath(string targetPath, bool isDirectory)
        {
            if (string.IsNullOrEmpty(targetPath)) return false;
            try
            {
                string full = Path.GetFullPath(targetPath);
                if (isDirectory ? !Directory.Exists(full) : !File.Exists(full))
                {
                    return false;
                }
                if ((File.GetAttributes(full) & FileAttributes.ReparsePoint) != 0)
                {
                    return false;
                }
                if (!IsUnderProtectedRoot(full))
                {
                    return false;
                }

                FileSystemSecurity sec = isDirectory
                    ? (FileSystemSecurity)Directory.GetAccessControl(full, AccessControlSections.Access)
                    : (FileSystemSecurity)File.GetAccessControl(full, AccessControlSections.Access);

                AuthorizationRuleCollection rules = sec.GetAccessRules(true, true, typeof(SecurityIdentifier));
                if (rules == null || rules.Count == 0)
                {
                    return false;
                }

                foreach (AuthorizationRule rawRule in rules)
                {
                    FileSystemAccessRule rule = rawRule as FileSystemAccessRule;
                    if (rule == null) continue;
                    if (rule.AccessControlType != AccessControlType.Allow) continue;
                    if ((rule.FileSystemRights & UnsafeWriteRights) == 0) continue;

                    SecurityIdentifier sid = rule.IdentityReference as SecurityIdentifier;
                    if (sid == null || IsUnprivilegedSid(sid))
                    {
                        return false;
                    }
                }
                return true;
            }
            catch
            {
                return false;
            }
        }

        private static bool IsAdminProtectedFile(string filePath)
        {
            if (string.IsNullOrEmpty(filePath) || !File.Exists(filePath)) return false;
            string parentDir = Path.GetDirectoryName(filePath);
            return IsAdminProtectedPath(filePath, false) && IsAdminProtectedPath(parentDir, true);
        }

        private static string ResolvePythonW(string baseDir, bool elevated)
        {
            if (!elevated)
            {
                // 1. Local embedded or venv Python adjacent to PrismFX.exe (standard user only)
                string[] localCandidates =
                {
                    Path.Combine(baseDir, "python", "pythonw.exe"),
                    Path.Combine(baseDir, ".venv", "Scripts", "pythonw.exe"),
                    Path.Combine(baseDir, "venv", "Scripts", "pythonw.exe")
                };
                foreach (string c in localCandidates)
                {
                    if (File.Exists(c)) return c;
                }

                // 2. Explicit environment override (standard user only)
                string envPy = Environment.GetEnvironmentVariable("PRISMFX_PYTHONW");
                if (!string.IsNullOrEmpty(envPy) && File.Exists(envPy))
                {
                    return envPy;
                }
            }

            // 3. Build-time configured Python runtime (when elevated, must reside in an admin-protected path)
            if (!string.IsNullOrEmpty(BuildConfig.PythonW) && File.Exists(BuildConfig.PythonW))
            {
                if (!elevated || IsAdminProtectedFile(BuildConfig.PythonW))
                {
                    return BuildConfig.PythonW;
                }
            }

            // 4. Windows Registry PythonCore (HKLM-only + admin-protected when elevated; HKCU+HKLM when standard)
            string fromReg = FindPythonWInRegistry(elevated);
            if (!string.IsNullOrEmpty(fromReg)) return fromReg;

            // 5. PATH lookup (System Machine PATH + admin-protected only when elevated)
            string fromPath = FindInPath("pythonw.exe", elevated) ?? FindInPath("pyw.exe", elevated);
            return fromPath ?? string.Empty;
        }

        private static Version ParsePythonVersion(string raw)
        {
            if (string.IsNullOrEmpty(raw)) return new Version(0, 0);
            string cleaned = raw.Trim();
            int dash = cleaned.IndexOf('-');
            if (dash > 0)
            {
                cleaned = cleaned.Substring(0, dash);
            }
            Version parsed;
            if (Version.TryParse(cleaned, out parsed))
            {
                return parsed;
            }
            return new Version(0, 0);
        }

        private static string FindPythonWInRegistry(bool elevated)
        {
            var candidates = new List<KeyValuePair<string, string>>();
            RegistryKey[] hives = elevated
                ? new RegistryKey[] { Registry.LocalMachine }
                : new RegistryKey[] { Registry.CurrentUser, Registry.LocalMachine };
            string[] roots = { @"SOFTWARE\Python\PythonCore", @"SOFTWARE\WOW6432Node\Python\PythonCore" };

            foreach (RegistryKey hive in hives)
            {
                foreach (string root in roots)
                {
                    try
                    {
                        using (RegistryKey baseKey = hive.OpenSubKey(root))
                        {
                            if (baseKey == null) continue;
                            foreach (string ver in baseKey.GetSubKeyNames())
                            {
                                using (RegistryKey ip = baseKey.OpenSubKey(ver + @"\InstallPath"))
                                {
                                    if (ip == null) continue;
                                    string dir = ip.GetValue(null) as string;
                                    if (string.IsNullOrEmpty(dir)) continue;
                                    string pyw = Path.Combine(dir, "pythonw.exe");
                                    if (File.Exists(pyw) && (!elevated || IsAdminProtectedFile(pyw)))
                                    {
                                        candidates.Add(new KeyValuePair<string, string>(ver, pyw));
                                    }
                                }
                            }
                        }
                    }
                    catch
                    {
                    }
                }
            }

            candidates.Sort(delegate(KeyValuePair<string, string> a, KeyValuePair<string, string> b)
            {
                Version va = ParsePythonVersion(a.Key);
                Version vb = ParsePythonVersion(b.Key);
                int cmp = vb.CompareTo(va);
                if (cmp != 0) return cmp;
                bool a32 = a.Key.EndsWith("-32", StringComparison.OrdinalIgnoreCase);
                bool b32 = b.Key.EndsWith("-32", StringComparison.OrdinalIgnoreCase);
                if (a32 != b32) return a32 ? 1 : -1;
                return string.Compare(b.Key, a.Key, StringComparison.OrdinalIgnoreCase);
            });
            return candidates.Count > 0 ? candidates[0].Value : string.Empty;
        }

        private static string FindInPath(string exeName, bool elevated)
        {
            string pathEnv = elevated
                ? (Environment.GetEnvironmentVariable("PATH", EnvironmentVariableTarget.Machine) ?? string.Empty)
                : (Environment.GetEnvironmentVariable("PATH") ?? string.Empty);
            foreach (string rawDir in pathEnv.Split(';'))
            {
                string dir = rawDir.Trim().Trim('"');
                if (string.IsNullOrEmpty(dir)) continue;
                try
                {
                    string full = Path.Combine(dir, exeName);
                    if (File.Exists(full) && (!elevated || IsAdminProtectedFile(full)))
                    {
                        return full;
                    }
                }
                catch
                {
                }
            }
            return null;
        }

        private static string QuoteArg(string arg)
        {
            if (string.IsNullOrEmpty(arg)) return "\"\"";
            bool needsQuotes = false;
            foreach (char c in arg)
            {
                if (c == ' ' || c == '\t' || c == '"')
                {
                    needsQuotes = true;
                    break;
                }
            }
            if (!needsQuotes) return arg;

            var sb = new StringBuilder();
            sb.Append('"');
            int backslashes = 0;
            foreach (char c in arg)
            {
                if (c == '\\')
                {
                    backslashes++;
                }
                else if (c == '"')
                {
                    sb.Append('\\', backslashes * 2 + 1);
                    sb.Append('"');
                    backslashes = 0;
                }
                else
                {
                    if (backslashes > 0)
                    {
                        sb.Append('\\', backslashes);
                        backslashes = 0;
                    }
                    sb.Append(c);
                }
            }
            if (backslashes > 0)
            {
                sb.Append('\\', backslashes * 2);
            }
            sb.Append('"');
            return sb.ToString();
        }
    }
}
