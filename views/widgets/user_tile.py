from kivy.properties import StringProperty
from kivymd.uix.card import MDCard


class UserKeyTile(MDCard):
    room_name = StringProperty("")
    key_code = StringProperty("")
    status_text = StringProperty("")
    sub_status_text = StringProperty("")








