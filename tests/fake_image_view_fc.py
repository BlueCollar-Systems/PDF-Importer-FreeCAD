"""Native-shaped image view double: unlit rendering is a display mode."""
from types import SimpleNamespace


class Translation:
    def __init__(self):
        self.name = ""
        self.translation = SimpleNamespace(setValue=lambda *v: setattr(self, "value", v))

    def setName(self, value):
        self.name = value

    def getName(self):
        return self.name


class Root:
    def __init__(self):
        self.children = []

    def getNumChildren(self):
        return len(self.children)

    def getChild(self, index):
        return self.children[index]

    def removeChild(self, index):
        self.children.pop(index)

    def insertChild(self, node, index):
        self.children.insert(index, node)


class ImageView:
    def __init__(self):
        self.RootNode = Root()
        self.Visibility = False
        self._lighting = "One side"
        self._display_mode = "Shaded"

    @property
    def Lighting(self):
        return self._lighting

    @Lighting.setter
    def Lighting(self, value):
        if value not in ("One side", "Two side"):
            raise ValueError("Unsupported Image::ImagePlane Lighting enumeration")
        self._lighting = value

    @property
    def DisplayMode(self):
        return self._display_mode

    @DisplayMode.setter
    def DisplayMode(self, value):
        if value not in ("Shaded", "No shading"):
            raise ValueError("Unsupported Image::ImagePlane DisplayMode enumeration")
        self._display_mode = value
