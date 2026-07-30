from kivy.properties import StringProperty, BooleanProperty
from kivymd.uix.card import MDCard


class PermissionTile(MDCard):
    """Room/key tile with access state.

    Properties are defined so KV bindings like root.allowed work.
    """

    room_name = StringProperty("")  # long names are shortened in KV
    key_code = StringProperty("")
    allowed = BooleanProperty(False)
    # Customizable status labels (used in multiple screens)
    label_assigned = StringProperty("доступ есть")
    label_unassigned = StringProperty("нет допуска")
    hovered = BooleanProperty(False)
    pressed = BooleanProperty(False)
    # When True (default), clicking the tile toggles permission in admin UI.
    # For user UI, set to False to disable admin toggle behavior.
    allow_toggle = BooleanProperty(True)


