import re

def sanitize_filename(filename, replace_spaces=False):
    # 1. Remove invalid characters
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '', filename)
    
    # 2. Handle trailing spaces and periods
    safe_name = safe_name.rstrip('. ')
    
    # 3. Handle spaces (optional but recommended)
    if replace_spaces:
        safe_name = safe_name.replace(' ', '_')
        
    # 4. Check for Windows reserved names
    base_name = safe_name.split('.')[0].upper()
    reserved_names = {
        'CON', 'PRN', 'AUX', 'NUL',
        'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
        'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'
    }
    if base_name in reserved_names:
        safe_name = f"{safe_name}_safe"
        
    # 5. Truncate to 255 characters (keeping extension if possible)
    if len(safe_name) > 255:
        parts = safe_name.rsplit('.', 1)
        if len(parts) == 2:
            safe_name = f"{parts[0][:254-len(parts[1])]}.{parts[1]}"
        else:
            safe_name = safe_name[:255]
            
    # Fallback if empty
    return safe_name if safe_name else "unnamed_file"

# Example usage:
# print(sanitize_filename("My <Awesome> File?.txt")) # Output: My Awesome File.txt
# print(sanitize_filename("CON.txt"))                # Output: CON.txt_safe
print(sanitize_filename("Sekai Saikyou no Kishi wa, Kanarazu Shinu Heroine wo Sukuu Tame Isekai Demo Saikyou no Kishi to Naru ~Ryoutei ni Hana wo, Ryoute ni Ken wo~.epub"))