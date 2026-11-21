using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
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
        [STAThread]
        private static int Main(string[] args)
        {
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

            string pythonW = ResolvePythonW(baseDir);
            if (string.IsNullOrEmpty(pythonW))
            {
                MessageBox.Show(
                    "Python 3 (pythonw.exe) with PySide6 was not found on this machine.\n" +
                    "Please install Python 3.10+ or set PRISMFX_PYTHONW.",
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
            psi.EnvironmentVariables["PRISMFX_LAUNCHER_EXE"] = selfExe;

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

        private static string ResolvePythonW(string baseDir)
        {
            // 1. Local embedded or venv Python adjacent to PrismFX.exe
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

            // 2. Explicit environment override
            string envPy = Environment.GetEnvironmentVariable("PRISMFX_PYTHONW");
            if (!string.IsNullOrEmpty(envPy) && File.Exists(envPy))
            {
                return envPy;
            }

            // 3. Build-time verified Python runtime (works identically under Standard User, Admin UAC & Task Scheduler)
            if (!string.IsNullOrEmpty(BuildConfig.PythonW) && File.Exists(BuildConfig.PythonW))
            {
                return BuildConfig.PythonW;
            }

            // 4. Windows Registry PythonCore (HKCU then HKLM, newest version first)
            string fromReg = FindPythonWInRegistry();
            if (!string.IsNullOrEmpty(fromReg)) return fromReg;

            // 5. PATH lookup (pythonw.exe then pyw.exe)
            string fromPath = FindInPath("pythonw.exe") ?? FindInPath("pyw.exe");
            return fromPath ?? string.Empty;
        }

        private static string FindPythonWInRegistry()
        {
            var candidates = new List<KeyValuePair<string, string>>();
            RegistryKey[] hives = { Registry.CurrentUser, Registry.LocalMachine };
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
                                    if (File.Exists(pyw))
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

            candidates.Sort((a, b) => string.Compare(b.Key, a.Key, StringComparison.OrdinalIgnoreCase));
            return candidates.Count > 0 ? candidates[0].Value : string.Empty;
        }

        private static string FindInPath(string exeName)
        {
            string pathEnv = Environment.GetEnvironmentVariable("PATH") ?? string.Empty;
            foreach (string rawDir in pathEnv.Split(';'))
            {
                string dir = rawDir.Trim().Trim('"');
                if (string.IsNullOrEmpty(dir)) continue;
                try
                {
                    string full = Path.Combine(dir, exeName);
                    if (File.Exists(full)) return full;
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
