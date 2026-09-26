def classFactory(iface):
    from .plugin import KentuckyStacPlugin

    return KentuckyStacPlugin(iface)
