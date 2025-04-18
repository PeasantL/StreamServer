import json
import os

class ConfigManager:
    _instance = None
    
    def __new__(cls, config_file="config.json"):
        if cls._instance is None:
            cls._instance = object.__new__(cls)
            cls._instance.config_file = config_file
            cls._instance._config_data = None
            cls._instance._load_config()
            
            # Override with environment variables if they exist
            if os.environ.get('VIDEO_DIR'):
                cls._instance._config_data["video_dir"] = os.environ.get('VIDEO_DIR')
            if os.environ.get('THUMBNAIL_DIR'):
                cls._instance._config_data["thumbnail_dir"] = os.environ.get('THUMBNAIL_DIR')
            if os.environ.get('DB_FILE'):
                cls._instance._config_data["db_file"] = os.environ.get('DB_FILE')
                
        return cls._instance
    
    def _load_config(self):
        """Load the config file into memory"""
        try:
            with open(self.config_file, 'r') as f:
                self._config_data = json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Config file '{self.config_file}' not found")
        except json.JSONDecodeError:
            raise ValueError(f"Invalid JSON in config file '{self.config_file}'")

    def _save_config(self):
        """Save the current config data to file"""
        with open(self.config_file, 'w') as f:
            json.dump(self._config_data, f, indent=4)

    @property
    def video_dir(self):
        return self._config_data.get("video_dir")
    
    # Add to ConfigManager class
    @property
    def parent_dir(self):
        return os.environ.get('PARENT_DIR', os.path.dirname(self.video_dir))


    @video_dir.setter
    def video_dir(self, value):
        self._config_data["video_dir"] = str(value)
        self._save_config()

    @property
    def allowed_ips(self):
        return self._config_data.get("allowed_ips", [])

    @property
    def thumbnail_dir(self):
        return self._config_data.get("thumbnail_dir")

    @property
    def db_file(self):
        return self._config_data.get("db_file")

    def reload(self):
        self._load_config()

    def save(self):
        self._save_config()

# Module-level instance that will be shared
config = ConfigManager()
