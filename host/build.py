import PyInstaller.__main__
import os

if __name__ == '__main__':
    host_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(host_dir, 'claude_monitor_daemon.py')
    
    PyInstaller.__main__.run([
        script_path,
        '--onefile',
        '--noconsole',
        '--clean',
        '--distpath', os.path.join(host_dir, 'dist'),
        '--workpath', os.path.join(host_dir, 'build'),
        '--specpath', host_dir,
        '--name', 'claude_monitor_daemon'
    ])
