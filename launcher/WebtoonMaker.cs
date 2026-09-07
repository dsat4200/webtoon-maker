using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

internal static class WebtoonMaker
{
    // Quote each argument for Windows CreateProcess, without invoking a shell.
    private static string Quote(string value)
    {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value)
        {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') result.Append('\\', slashes * 2 + 1);
            else result.Append('\\', slashes);
            result.Append(c);
            slashes = 0;
        }
        return result.Append('\\', slashes * 2).Append('"').ToString();
    }

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            string python = File.ReadAllText(Path.Combine(root, "WebtoonMaker.python.txt")).Trim();
            if (!File.Exists(python)) throw new FileNotFoundException("Python was moved. Run build-launcher.ps1 again.", python);
            string script = Path.Combine(root, "main.py");
            if (!File.Exists(script)) throw new FileNotFoundException("Keep WebtoonMaker.exe beside main.py.", script);
            var arguments = new StringBuilder(Quote(script)).Append(" --");
            // Resolve relative paths before changing the working directory.
            foreach (string arg in args) arguments.Append(" ").Append(Quote(Path.GetFullPath(arg)));
            Process.Start(new ProcessStartInfo {
                FileName = python, Arguments = arguments.ToString(),
                WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true
            });
            return 0;
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "Webtoon Maker", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
